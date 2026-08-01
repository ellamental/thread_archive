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
    The **peak** number of retrieval calls in flight **in this process** at any
    moment during this one. The MCP server is long-lived and serves a client that
    pipelines, so self-contention is real and entirely invisible to a per-call
    timer. A peak rather than an instantaneous reading because a search runs for
    seconds and its peers arrive throughout it: the count at any single instant —
    the start most of all — routinely misses every one of them.

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

``uptime_s``
    How long the serving process had been alive. Every cache retrieval leans on —
    the vector matrix, the embedding model, the exact-set memo,
    SQLite's page cache — is process-local and starts empty, so the same query
    against the same corpus costs an order of magnitude more at second five than at
    second five hundred. Without this the two are the same row, and *every*
    before/after comparison over the ledger silently compares cache states instead
    of code.

    It doubles as process identity, which is why it is a duration and not a
    boolean: ``at - uptime_s`` is the process's start, so records sharing one value
    came from one process, and a threshold for "cold" can be chosen when the
    question is asked rather than baked in when it is recorded.

``load1``
    The machine's one-minute load average. Every other signal here is scoped to
    this process, and most of what competes for this box is not: a test suite, a
    sweep of the family's CI, a second agent's session, an editor indexing. The
    same search measures milliseconds on a quiet box and seconds beside any of
    them, and without this the two are the same row — which makes *every* latency
    percentile over the ledger a mixture of the pipeline and the afternoon.

    Raw rather than divided by the core count: it is the number the operator
    already reads off ``uptime``, and cores are a property of the machine the
    home lives on rather than of a call it served. The one-minute window is the
    shortest the kernel keeps and still averages over more than a fast search;
    for a slow one — which is the case worth diagnosing — it covers most of it.

``rss_mb`` / ``rss_now_mb``
    Peak resident memory of the serving process, and what it holds at this instant.
    The embedding model is hundreds of megabytes and the vector pack is a gigabyte
    the matvec streams end to end, so a process serving search is the largest thing
    on the box, and the point where the machine starts swapping is a latency
    finding that no timer can see.

    Both, because the gap between them is the finding. Peak is a high-water mark
    since process start and never falls, so it answers whether this process has
    ever been big enough to hurt the machine it shares — but for the same reason it
    cannot say whether the memory is *currently* held. A warmed long-lived server
    that peaked at 3 GB and is resident at 45 MB has been evicted to swap, and its
    next query pays to fault the vector matrix back in: measured on this archive,
    the same query costs seconds on that first search and a few hundred
    milliseconds on the one after it, with every other warmth signal reading warm
    throughout — ``uptime_s`` in the hours, ``cold`` and ``embed_cold`` both false,
    because the models are constructed and only their pages are gone. The current
    reading is the only one of these that sees it.

The remaining fields are omitted unless they say something (no in-flight peers, no
refresh, a long-quiet WAL), so a search on an idle machine records nothing and
their presence carries the signal. That economy has a cost worth naming: an absent
field means *nothing to report*, never *not measured*, and the two are only the
same as long as every caller that competes for the machine enters the in-flight
span — not only the ones serving a request. A warm pass loads a model and runs a
real search, and is the heaviest thing a process ever does; a caller that samples
without entering makes its own work invisible to everyone else's peak, and the
ledger then reads idle on a machine that was not.

``uptime_s``, ``load1``, ``rss_mb`` and ``rss_now_mb`` are the exceptions and are
always present: there is no reading of any of them that means *nothing to report*,
and they are the denominators the others are read against — the process's cache
state, the machine's own busyness, and what this process is costing it.
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

#: When this process started, on the monotonic clock. Module import is close enough
#: to process start for the question this answers (was anything cached yet), and it
#: is the one moment guaranteed to precede every search the process serves — a
#: wall-clock read would drift with the system clock and make two records from one
#: process disagree about when it started.
_STARTED = time.monotonic()

_INFLIGHT_LOCK = threading.Lock()
_inflight = 0
#: The spans currently in flight, so an arriving call can raise the peak of the
#: calls already running. Bounded by real concurrency (a handful), and every entry
#: is removed in a ``finally``.
_SPANS: list["Span"] = []

#: When retrieval last finished work in this process, on the monotonic clock.
#: Initialised to process start so a server that has served nothing reads as idle
#: for its whole life rather than as having just worked.
_last_activity = _STARTED


class Span:
    """One call's in-flight span, carrying the **peak** concurrency it saw.

    An instantaneous count taken when a search begins answers the wrong question.
    A search runs for seconds; its peers arrive throughout, and the ones that
    arrive *after* it started are exactly the ones a start-of-request sample can
    never see. Recorded traffic bears this out — a ledger with plenty of provably
    overlapping calls in which the instantaneous field had never once fired.

    So the span carries a high-water mark instead: every call that enters bumps the
    counter and raises the peak of everyone currently in flight, and each reads its
    own peak on the way out. A search that started alone and finished in a crowd
    reports the crowd.
    """

    __slots__ = ("peak",)

    def __init__(self, peak: int) -> None:
        self.peak = peak


@contextmanager
def in_flight() -> Iterator[Span]:
    """Count this call as in flight for its duration, yielding its :class:`Span`.

    Wraps the retrieval work at the surface, so the count includes the caller
    itself — a lone search peaks at ``1``, which is why the field is only recorded
    above that. Pass the span to :func:`sample` at the *end* of the work to fold
    its peak into the record.
    """
    global _inflight
    with _INFLIGHT_LOCK:
        _inflight += 1
        span = Span(_inflight)
        # Everyone currently in flight now has one more peer than they did, and
        # that is true of them whether or not they are the one arriving.
        for other in _SPANS:
            if _inflight > other.peak:
                other.peak = _inflight
        _SPANS.append(span)
    try:
        yield span
    finally:
        global _last_activity
        with _INFLIGHT_LOCK:
            _inflight -= 1
            _last_activity = time.monotonic()
            try:
                _SPANS.remove(span)
            except ValueError:  # never let bookkeeping break a search
                pass


def inflight_now() -> int:
    """Retrieval calls in flight in this process at this instant.

    The instantaneous reading :class:`Span` deliberately does not record, exposed
    for the one caller the distinction suits: a background task deciding whether to
    do optional work *now* wants to know whether it would be competing, not what
    the peak was."""
    with _INFLIGHT_LOCK:
        return _inflight


def idle_s() -> float:
    """Seconds since retrieval last finished work in this process."""
    return time.monotonic() - _last_activity


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


def peak_inflight(span: Optional[Span]) -> dict[str, Any]:
    """The concurrency field, read off a finished :func:`in_flight` span.

    Split out from :func:`sample` because the two are taken at opposite ends of the
    work and for opposite reasons. The sampled facts describe *what the machine was
    doing when this call began* — a rebuild that finished mid-search still shaped
    the search, and an end sample would report it absent. Concurrency is the other
    way round: it is only known once the call is over, because the peers that make
    a search slow include the ones that arrived while it ran.

    Merge into the sampled record just before it is written."""
    rec: dict[str, Any] = {}
    try:
        if span is not None and span.peak > 1:
            rec["inflight"] = span.peak
    except Exception:  # noqa: BLE001 — advisory; never break a search
        logger.debug("could not read in-flight peak", exc_info=True)
    return rec


def sample() -> dict[str, Any]:
    """The contention facts worth recording, omitting the ones that say nothing —
    except ``uptime_s``, which every reading of is worth knowing.

    Taken at the *start* of the work; the concurrency field is not among them and
    arrives separately from :func:`peak_inflight`."""
    rec: dict[str, Any] = {"uptime_s": round(time.monotonic() - _STARTED, 1)}
    try:
        from .._ops import machine

        load = machine.load1()
        if load is not None:
            rec["load1"] = load
        rss = machine.rss_mb()
        if rss is not None:
            rec["rss_mb"] = rss
        rss_now = machine.rss_now_mb()
        if rss_now is not None:
            rec["rss_now_mb"] = rss_now
        busy = _refreshing()
        if busy:
            rec["refreshing"] = busy
        age = _wal_age_s()
        if age is not None and age <= _WAL_STALE_S:
            rec["wal_age_s"] = age
    except Exception:  # noqa: BLE001 — context is advisory; a search must not fail for it
        logger.debug("could not sample contention", exc_info=True)
    return rec
