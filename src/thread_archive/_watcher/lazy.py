"""Lazy catch-up ingest: a one-shot, cross-process-safe watch pass.

This is the explicitly opted-in zero-daemon freshness path:
``THREAD_ARCHIVE_MCP_INGEST=1 archive-mcp`` runs a pass in a background thread
at startup and (throttled) around tool calls. Setup-generated stdio client
entries carry that opt-in. A bare server stays read-only, and the always-on
watcher remains the always-fresh upgrade.

Exactly one process ingests at a time. The pass runs only while holding a
non-blocking exclusive flock on ``<home>/.ingest-owner.lock``; the watcher
daemon (:meth:`.daemon.Watcher.run`) holds the same lock for its whole
lifetime. So with the daemon installed every lazy pass degrades to a no-op
flock probe, and with several MCP servers alive concurrently (parallel
Claude Code sessions) only one of them catches up — the rest skip, and
whatever they would have imported is picked up by the next pass. Skipping
loses nothing: sources replay from their own import state. flock
auto-releases on process death, so a killed holder can never wedge ingest.

Within the pass, writes hold the shared reindex lock (see
:func:`.._truth.shared_ingest_lock`) like every other cross-process writer.
"""

from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .base import WatchResult

logger = logging.getLogger(__name__)

INGEST_OWNER_LOCK_FILE = ".ingest-owner.lock"


def _lock_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / INGEST_OWNER_LOCK_FILE


def acquire_ingest_owner() -> Optional[int]:
    """Take the ingest-owner lock (exclusive, non-blocking).

    Returns the open fd while held — the caller owns closing it, which
    releases the lock — or ``None`` when another process holds it. The
    watcher daemon keeps the fd open for its lifetime; a lazy pass closes it
    when the pass ends.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextmanager
def try_ingest_owner_lock() -> Generator[bool, None, None]:
    """Hold the ingest-owner lock for one pass, non-blocking. Yields True
    holding the lock, or False (not holding) when the daemon or another lazy
    pass owns ingest — the caller skips."""
    fd = acquire_ingest_owner()
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)  # closing the fd releases the flock


def catch_up_once(
    home: Optional[str] = None,
    *,
    watchers: Optional[list] = None,
    embed: bool = True,
    embed_batch: int = 256,
) -> Optional[WatchResult]:
    """One catch-up ingest pass, or ``None`` when another process owns ingest.

    Polls every available source, runs the one-shot maintenance pass when
    anything was imported, and embeds one bounded batch of missing vectors
    (a no-op without the ``[embeddings]`` extra) — the same work a daemon
    poll cycle does, shaped for a process that isn't sticking around. A
    vector backlog larger than one batch drains across successive passes.
    """
    with try_ingest_owner_lock() as owned:
        if not owned:
            return None

        from .._lifecycle import open_archive
        from .._truth import shared_ingest_lock
        from .daemon import Watcher

        open_archive(home)
        watcher = Watcher(watchers, home=home, embed=embed, embed_batch=embed_batch)
        # Blocking shared lock, like `watch --once`: a one-shot pass has no
        # later cycle to retry on, so it waits out an in-flight reindex.
        with shared_ingest_lock():
            result = watcher.poll_once()
            if result.events_created > 0:
                watcher.maintain()
            if embed:
                try:
                    watcher.embed_pending()
                except Exception as e:  # noqa: BLE001 — vectors are best-effort here
                    logger.warning("lazy ingest: embed error: %s", e)
        if result.events_created > 0:
            logger.info(
                "lazy ingest: imported %d events (%d items)",
                result.events_created, result.items_imported,
            )
        return result
