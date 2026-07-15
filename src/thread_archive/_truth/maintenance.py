"""Checkpoint + shard rebalance: the truth directory's maintenance cadence.

``checkpoint`` persists the cross-thread overlays and the thread-metadata
backstop and keeps the shard layout balanced; ``_maybe_rebalance`` is the
crash-safe, merge-only, serialized sweep. Storage-model overview:
:mod:`.jsonl_log`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .._store import ImportState, Thread, get_session
from . import drain
from .layout import (
    THREADS_SUBDIR,
    _CROSS_THREAD,
    _depth_for,
    _fsync_dir,
    _json_default,
    _now_iso,
    _read_manifest,
    _row_dict,
    _thread_file,
    log_dir,
    update_manifest,
)
from .locks import (
    _truth_write_lock,
    _try_rebalance_lock,
    shared_ingest_lock,
    try_shared_ingest_lock,
)

logger = logging.getLogger(__name__)

# ── checkpoint (cross-thread snapshots + metadata-update backstop) ───────────
def _write_snapshot(d: Path, name: str, model: type) -> int:
    """Atomically rewrite ``<name>.jsonl`` from the live table (tmp + rename)."""
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    with get_session() as s, open(tmp, "w", encoding="utf-8") as fh:
        n = 0
        for obj in s.execute(select(model)).scalars():
            fh.write(json.dumps(_row_dict(obj), default=_json_default, ensure_ascii=False))
            fh.write("\n")
            n += 1
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename makes it visible
    os.replace(tmp, path)
    _fsync_dir(d)  # the rename itself must survive power loss
    return n


def _checkpoint_changed_threads(d: Path, depth: int, last_iso: str | None) -> int:
    """Backstop for thread *metadata updates*: append a fresh thread record for any
    thread whose ``updated_at`` advanced since the last checkpoint. New threads are
    already recorded at creation (:func:`record_thread`), so the first checkpoint
    (no watermark) writes nothing — it just sets the watermark."""
    if last_iso is None:
        return 0
    last_dt = datetime.fromisoformat(last_iso)
    n = 0
    touched: set[Path] = set()
    with get_session() as s:
        changed = s.execute(select(Thread).where(Thread.updated_at > last_dt)).scalars().all()
        with _truth_write_lock():  # append batches are mutually exclusive across writers
            for t in changed:
                path = _thread_file(d, t.id, depth)
                drain._append_line(path, {"type": "thread", **_row_dict(t)})
                touched.add(path)
                n += 1
            for path in touched:
                drain._fsync_handle(path)
    return n


def checkpoint(*, snapshots: bool = True) -> dict:
    """Persist the cross-thread overlays + any thread-metadata updates, and keep the
    shard layout balanced. Per-thread event/metadata records are written inline on
    commit; this is the cadence call (and pre-backup call) that makes the directory
    a complete restore set.

    ``snapshots=False`` runs **maintenance only** — the manifest watermark, the
    thread-metadata-update backstop, the (small) ``import_state`` snapshot, and
    shard rebalance — and skips the full
    rewrite of the cross-thread overlay files (``thread_links`` / ``topic_messages``).
    That rewrite is O(graph size) and the live-ingest path never changes those tables
    (conversation import only appends events; the overlays are the migration seed plus
    the append-only ``kg_events`` log), so the watcher uses the cheap maintenance form
    on its cadence instead of rewriting tens of MB every few seconds. The full form
    (overlays included) is for explicit pre-backup / pre-reindex / post-curation use.

    Self-locking: the truth appends + manifest watermark advance here must never
    race a reindex, so the shared ingest lock is taken *inside* — a caller
    already holding it shared just nests (flock SH + SH coexist)."""
    with shared_ingest_lock():
        return _checkpoint_locked(snapshots=snapshots)


def _checkpoint_locked(*, snapshots: bool = True) -> dict:
    d = log_dir()
    (d / THREADS_SUBDIR).mkdir(parents=True, exist_ok=True)
    m = _read_manifest(d)
    counts: dict = (
        {name: _write_snapshot(d, name, model) for name, model in _CROSS_THREAD.items()}
        if snapshots
        else {}
    )
    # Source-import watermarks: operational state, snapshotted so a reindex of a
    # lost/deleted index (where there is no previous index to carry them from)
    # still restores cursors instead of adopting active sources at EOF. A stale
    # snapshot is safe (import resumes from the older watermark and dedup_key
    # collapses the overlap), but a *fresh* one keeps the regression window at
    # minutes instead of a backup cycle — and the table is small, so unlike the
    # cross-thread overlays it is snapshotted on the maintenance cadence too.
    counts["import_state"] = _write_snapshot(d, "import_state", ImportState)
    # Rebalance BEFORE the thread-metadata backstop, so the backstop appends at the
    # post-rebalance depth and can never manufacture a flat twin of a just-moved file.
    depth = _maybe_rebalance(d, int(m.get("shard_depth", 0)))
    # Re-read the manifest: a concurrent sweep (ours skips when the rebalance lock is
    # held) may have advanced shard_depth — never write a stale depth back over it.
    m = _read_manifest(d)
    depth = max(depth, int(m.get("shard_depth", 0)))
    # The new watermark is captured BEFORE the changed-threads query: a thread
    # updated between the query and a later stamp would fall below the stamp and
    # be missed by every later backstop pass. Captured-first, such an update is
    # re-scanned next pass; the worst case is a harmless re-appended thread record
    # (latest-wins). Updates whose transaction is still uncommitted when the query
    # runs are the primary seam's job — every in-tree metadata writer also stages
    # its record inline (``record_thread``); this pass is only the backstop.
    checkpoint_started_at = _now_iso()
    counts["threads_updated"] = _checkpoint_changed_threads(d, depth, m.get("last_checkpoint_at"))
    # Locked read-modify-write: the backstop pass takes time, and the manifest
    # is shared state (shard depth from a concurrent sweep, a verify run's
    # hashes baseline). Mutating only this checkpoint's own keys under the
    # manifest lock means a foreign key written mid-pass survives.

    def _stamp(m: dict) -> None:
        m["shard_depth"] = max(depth, int(m.get("shard_depth", 0)))
        m["last_checkpoint_at"] = checkpoint_started_at

    update_manifest(d, _stamp)
    logger.info("jsonl_log checkpoint(snapshots=%s): %s", snapshots, counts)
    return counts


def _merge_file_into(src: Path, dest: Path) -> None:
    """Append ``src``'s lines onto ``dest`` (fsynced), then remove ``src`` — the
    no-clobber move for a file whose home already exists.

    Safe at every boundary: if ``dest`` ends in a torn line (a crash mid-append),
    a newline is inserted first so the fragment stays its own unparseable line
    instead of gluing onto ``src``'s first record. Duplicate lines from a crash
    between copy and unlink are harmless — event lines are id-keyed (reindex's
    INSERT OR REPLACE collapses them) and thread records are latest-wins. The
    copy re-checks ``src``'s size before unlinking so lines a racing pre-bump
    commit appended mid-copy are drained, not dropped."""
    with open(dest, "ab") as df:
        if os.stat(dest).st_size > 0:
            with open(dest, "rb") as ck:
                ck.seek(-1, os.SEEK_END)
                if ck.read(1) != b"\n":
                    df.write(b"\n")
        with open(src, "rb") as sf:
            while True:
                shutil.copyfileobj(sf, df)
                df.flush()
                if sf.tell() >= os.stat(src).st_size:
                    break  # nothing landed on src during the copy
        os.fsync(df.fileno())
    os.unlink(src)


def _maybe_rebalance(d: Path, depth: int) -> int:
    """Keep the shard layout balanced — crash-safe, merge-only, serialized.

    Three properties make a killed or concurrent sweep unable to lose truth:

    * **Manifest first.** Crossing a threshold persists the new ``shard_depth``
      *before* any file moves. Writers re-read the manifest on every commit, so
      they immediately compute paths in the final layout — a crash mid-sweep
      leaves only not-yet-moved files, never a growing flat twin of an
      already-moved one.
    * **Merge, never clobber.** A file whose destination already exists (a twin
      left by a killed sweep, or by a commit that raced the manifest bump) is
      appended onto it via :func:`_merge_file_into` — recombined, not replaced.
    * **Straggler sweep.** Once sharded (``depth > 0``), every call re-homes any
      misplaced file, so an interrupted migration is finished by the next
      checkpoint rather than waiting on a threshold that will never re-fire.

    The sweep skips (without moving anything) while a reindex build holds the
    ingest lock exclusive, and runs under its own non-blocking exclusive flock so
    two checkpointing processes can't interleave moves — the loser returns the
    depth it was given and its caller re-reads the manifest rather than writing a
    stale depth back. Returns the effective shard depth."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return depth
    n = sum(1 for _ in threads_dir.rglob("*.jsonl"))
    if max(depth, _depth_for(n)) == 0:
        return 0  # flat and staying flat — nothing can be misplaced
    with try_shared_ingest_lock() as ingest_ok:
        if not ingest_ok:
            return depth  # a reindex is reading the truth — don't move it underneath
        with _try_rebalance_lock() as held:
            if not held:
                return depth  # another sweep is running; it owns the manifest depth
            m = _read_manifest(d)
            depth = max(depth, int(m.get("shard_depth", 0)))  # fresh under the lock
            target = max(depth, _depth_for(n))
            if target > depth:
                # Locked RMW, durable BEFORE any file moves (see docstring).
                update_manifest(d, lambda m: m.__setitem__(
                    "shard_depth", max(target, int(m.get("shard_depth", 0)))
                ))
            # List under the lock — a sweep that completed between the count above
            # and our acquisition has already re-homed what we would move again.
            misplaced: list[tuple[Path, Path]] = []
            for path in threads_dir.rglob("*.jsonl"):
                try:
                    tid = int(path.stem)
                except ValueError:  # pragma: no cover — stray file
                    continue
                dest = _thread_file(d, tid, target)
                if dest != path:
                    misplaced.append((path, dest))
            if not misplaced:
                return target
            drain.reset_handles()  # our own cached appenders must not span the sweep
            # The truth-write mutex excludes every drain for the move loop, so no
            # append can land on a file between its merge-copy and its unlink.
            with _truth_write_lock():
                for path, dest in misplaced:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        _merge_file_into(path, dest)
                    else:
                        os.replace(path, dest)
            logger.info(
                "jsonl_log rebalance: shard_depth %d → %d (%d files re-homed, %d threads)",
                depth, target, len(misplaced), n,
            )
    return target
