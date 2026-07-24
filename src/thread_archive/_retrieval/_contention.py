"""What else was competing for the machine when a retrieval call ran.

A latency number on its own is not diagnostic. The usage ledger records that a
search took seven seconds; nothing in it says whether the machine was idle at the
time or whether ingest was mid-write, a vector pack was rebuilding on a background
thread, and three other searches were in flight. Those are wildly different
findings — one is a slow pipeline, the other is a busy machine — and without the
context they are the same row.

This is deliberately a *sample of observable facts*, not an attempt at attribution.
Three signals, each cheap enough to take on every search:

``inflight``
    Retrieval calls concurrently in flight **in this process**. The MCP server is
    long-lived and serves a client that pipelines, so self-contention is real and
    entirely invisible to a per-call timer.

``refreshing``
    Background rebuilds running in this process — the vector matrix and the corpus
    graph both refresh single-flight on daemon threads. Each competes with the
    request path for CPU and page cache, and a graph build is measured in seconds.

``wal_age_s``
    Seconds since anything last wrote to the index, read off the SQLite WAL's mtime.
    This is the one **cross-process** signal here, and it is what makes the search
    ledger joinable to the ingest side without any coordination between them: reads
    never touch the WAL, so a WAL written moments ago means some other process — the
    watcher, an import, an embed drain — is actively writing the database this
    search is reading. Absent when there is no WAL or it can't be stat'd.

Every field is omitted unless it says something (no in-flight peers, no refresh, a
long-quiet WAL), so a search on an idle machine records nothing and the fields'
presence carries the signal.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: Above this, a WAL write is old enough that it tells us nothing about *this*
#: call — the point is naming concurrent writes, not dating the last one.
_WAL_STALE_S = 60.0

_INFLIGHT_LOCK = threading.Lock()
_inflight = 0


@contextmanager
def in_flight() -> Iterator[None]:
    """Count this call as in flight for its duration.

    Wraps the retrieval work at the surface, so ``sample()`` taken inside sees the
    caller itself included — a lone search reports ``1``, which is why the field is
    only recorded above that.
    """
    global _inflight
    with _INFLIGHT_LOCK:
        _inflight += 1
    try:
        yield
    finally:
        with _INFLIGHT_LOCK:
            _inflight -= 1


def _wal_age_s() -> Optional[float]:
    """Seconds since the index's WAL was last written, or None if unavailable."""
    try:
        from .._config import resolve_paths

        wal = str(resolve_paths().index_path) + "-wal"
        return round(max(0.0, time.time() - os.stat(wal).st_mtime), 1)
    except Exception:  # noqa: BLE001 — advisory; never break a search
        return None


def _refreshing() -> list[str]:
    """Which background rebuilds are in flight in this process."""
    busy: list[str] = []
    try:
        from . import vectors

        if vectors.is_refreshing():
            busy.append("matrix")
    except Exception:  # noqa: BLE001 — the vector extra may not be installed
        pass
    try:
        from . import embed_graph

        if embed_graph.is_refreshing():
            busy.append("graph")
    except Exception:  # noqa: BLE001
        pass
    return busy


def sample() -> dict[str, Any]:
    """The contention facts worth recording, omitting the ones that say nothing."""
    rec: dict[str, Any] = {}
    try:
        if _inflight > 1:
            rec["inflight"] = _inflight
        busy = _refreshing()
        if busy:
            rec["refreshing"] = busy
        age = _wal_age_s()
        if age is not None and age <= _WAL_STALE_S:
            rec["wal_age_s"] = age
    except Exception:  # noqa: BLE001 — context is advisory; a search must not fail for it
        logger.debug("could not sample contention", exc_info=True)
    return rec
