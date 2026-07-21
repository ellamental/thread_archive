"""The truth store's cross-process locks — all three flock families.

* the **truth-write mutex** (``.truthwrite.lock``): append batches are mutually
  exclusive across processes; acquiring it also resolves any crashed drain's
  leftover intent (lazily via :mod:`.drain`, which layers above this module);
* the **reindex quiesce lock** (``.reindex.lock``): writers hold it shared per
  ingest pass, reindex holds it exclusive across build+swap;
* the **rebalance lock** (``.rebalance.lock``): serializes the shard move/merge
  sweep, non-blocking.

Storage-model overview: :mod:`.jsonl_log`.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .._store import reconnect_if_swapped

# ── truth-write mutex (append batches are mutually exclusive across processes) ─
# The reindex lock is *shared* among writers, so two processes can append to the
# same truth file concurrently — a JSON line larger than one write(2) can interleave
# with another writer's line (corrupting both), and the drain-failure rollback
# truncates to a baseline that would chop records another process appended in
# between. This exclusive flock makes each append batch (a drain, a checkpoint
# backstop pass) atomic with respect to other writers. Held for milliseconds;
# writers are few. flock auto-releases on process death.
TRUTH_WRITE_LOCK_FILE = ".truthwrite.lock"


def _truth_write_lock_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / TRUTH_WRITE_LOCK_FILE


@contextmanager
def _truth_write_lock() -> Generator[None, None, None]:
    """Hold the truth-write mutex (exclusive flock), resolving any crashed
    drain's leftover intent first.

    The exclusion is cross-process AND cross-thread: each acquisition opens its
    own fd, and flock locks conflict between separate open file descriptions
    even within one process. That in-process serialization is load-bearing —
    the drain's module state (the ``_handles`` LRU, the intent file) has no
    threading.Lock of its own and relies on this mutex for it."""
    path = _truth_write_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # A leftover intent is a dead process's crashed batch — resolve it before
        # this holder appends (or moves files) on top of the partial tail.
        # Imported lazily: drain layers above this module (it imports the lock),
        # so a module-level import here would be circular.
        from .drain import _recover_crashed_drain

        _recover_crashed_drain()
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


# ── adaptive rebalance (flat → sharded when a directory gets large) ──────────
# Serializes the move/merge sweep across processes (the watcher's maintenance pass
# vs. the daily backup's checkpoint): flock, non-blocking — the loser skips, and
# whichever checkpoint runs next finishes the job. Deliberately distinct from the
# reindex lock: the watcher holds THAT one shared around its whole ingest pass, so
# an exclusive acquire on it here would self-deadlock.
REBALANCE_LOCK_FILE = ".rebalance.lock"


def _rebalance_lock_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / REBALANCE_LOCK_FILE


@contextmanager
def _try_rebalance_lock() -> Generator[bool, None, None]:
    """Hold the rebalance lock exclusive, non-blocking. Yields False (not holding)
    when another process is mid-sweep. flock auto-releases on process death — a
    killed sweep can never wedge the next one."""
    path = _rebalance_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing the fd releases the flock



# ── reindex quiesce lock (shared with the watcher's ingest pass) ─────────────
# flock on <home>/.reindex.lock: the watcher holds it SHARED for the duration of
# each ingest pass; reindex holds it EXCLUSIVE across build+swap. So a reindex
# waits out an in-flight pass, and no event can land in the truth mid-rebuild and
# silently miss the new index. flock auto-releases on process death — a killed
# holder can never wedge the other side.
REINDEX_LOCK_FILE = ".reindex.lock"


def _reindex_lock_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / REINDEX_LOCK_FILE


@contextmanager
def try_shared_ingest_lock() -> Generator[bool, None, None]:
    """Hold the reindex lock *shared* for one ingest pass, non-blocking.

    Yields True holding the lock, or False (not holding) when a reindex holds it
    exclusive — the caller skips the pass and ingest resumes on the next one, after
    the swap. Sources replay from their own import state, so a skipped pass loses
    nothing."""
    path = _reindex_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        # A reindex may have swapped index.db since this process last looked;
        # holding the shared lock, the identity observed here holds until
        # release, so reconnecting now is exactly once and exactly right.
        reconnect_if_swapped()
        yield True
    finally:
        os.close(fd)  # closing the fd releases the flock


@contextmanager
def shared_ingest_lock() -> Generator[None, None, None]:
    """Hold the reindex lock *shared*, blocking — for one-shot writers.

    The watcher uses the non-blocking :func:`try_shared_ingest_lock` (it can just
    skip a pass); a one-shot writer — a CLI import, a curation mutation — has no
    later pass to retry on, so it waits out an in-flight reindex instead. Every
    cross-process writer must hold this (or the try- variant) around its truth
    append **and** the SQLite commit: an unlocked write can append truth after the
    rebuild's read point and commit into the database inode the swap replaces —
    present in the truth, silently absent from the new index.

    Acquire also runs the index-swap reconnect: the blocking wait is precisely
    the window in which a reindex swaps ``index.db``, so an engine bound before
    the wait points at the orphaned pre-swap inode by the time the lock is
    granted. Nested shared holds are safe (flock SH coexists with SH, same
    process included), so helpers like :func:`checkpoint` take this lock
    themselves rather than trusting callers to."""
    path = _reindex_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        reconnect_if_swapped()
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


@contextmanager
def _hold_reindex_lock() -> Generator[None, None, None]:
    """Hold the reindex lock *exclusive* for build+swap (blocks until any
    in-flight ingest pass — a shared holder — finishes, bounded by one pass)."""
    path = _reindex_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)

