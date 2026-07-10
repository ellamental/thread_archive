"""JSONL truth-log: the durable source of truth for the archive.

The storage model is **JSONL-as-truth + SQLite-as-rebuildable-index**: the JSONL
directory on disk is the only authoritative store, and ``index.db`` is a pure
projection of it — ``rm index.db && archive reindex`` reconstructs the whole index
from the JSONL, and ``cp``/``rsync`` of the directory *is* the backup. Nothing
durable lives only in SQLite.

**One file per thread.** Each conversation (and each topic — topics are threads
too) is its own ``threads/<id>.jsonl``, the same shape as a Claude Code session
file: a ``{"type": "thread", ...}`` metadata record (latest wins) followed by one
``{"type": "event", ...}`` line per event, in order. A new event appends to that
one thread's file; you can open, diff, or ``cp`` a single conversation. The two
cross-thread overlays — ``thread_links`` (the topic graph's edges) and
``topic_messages`` (topic evidence) — aren't any one conversation's content, so
they stay as snapshot files (``thread_links.jsonl`` / ``topic_messages.jsonl``).

**Adaptive sharding.** A flat ``threads/`` directory is comfortable to ~16k files;
past that, tooling (file trees, ``ls``, completion) drags. ``manifest.json`` records
a ``shard_depth``: 0 = flat (``threads/<id>.jsonl``), 1 = 256 id-bucketed subdirs
(``threads/<id%256>/<id>.jsonl``, good to a few million), 2 = two levels. The path
resolver computes a thread's location from the recorded depth, so reads and writes
always agree; crossing the threshold triggers an auto-rebalance, and once sharded
every checkpoint re-homes any straggler file. The rebalance is crash-safe by
construction: the new depth is persisted *before* any file moves, a file whose home
already exists is merged (appended) rather than overwritten, and the sweep is
serialized across processes by a flock — see :func:`_maybe_rebalance`. Small
archives stay flat with zero ceremony.

Writes preserve the **JSONL ⊇ SQLite** invariant: a row is staged on the session
and flushed **and fsynced** to its thread file *before* the COMMIT it belongs to, so
the projection can never hold a row the truth lacks — the truth append meets the
same durability bar as SQLite's own WAL commit (plain ``fsync``, the same call
SQLite issues; neither uses ``F_FULLFSYNC``). :func:`reindex` is the recovery
primitive — it rebuilds the SQLite store from the JSONL directory as a
**build-and-swap**: the new index is built in a temp file and atomically renamed
over ``index.db``, so a killed reindex leaves the old index fully intact; a
corrupted or deleted index is never a data-loss event.
:func:`rebuild_truth_from_store` is the inverse: it re-emits the whole per-thread
truth from the current store (used once to migrate an older monolithic
``events.jsonl`` into per-thread files).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import sqlite3
from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from sqlalchemy import DateTime, event, insert, select
from sqlalchemy.orm import Session

from ..store import (
    Event,
    ImportState,
    KgEvent,
    Thread,
    ThreadLink,
    TopicMessage,
    build_engine,
    get_engine,
    get_session,
    init_db,
    use_engine,
)

logger = logging.getLogger(__name__)

# Per-thread files live under truth/threads/; the cross-thread overlays are
# snapshot files (full-rewritten at checkpoint) since they aren't one thread's
# content. thread_links / topic_messages FK threads.id, so they load after threads.
THREADS_SUBDIR = "threads"
_CROSS_THREAD: dict[str, type] = {"thread_links": ThreadLink, "topic_messages": TopicMessage}

# The curatorial event log (see KgEvent): an append-only file of every topic/link/
# evidence mutation. It is the *source of truth* for the knowledge layer — the
# thread_links / topic_messages projections are folded from it on reindex. Any
# legacy _CROSS_THREAD snapshot is treated as a reindex seed the replay reconciles
# on top (upsert + tombstone), so the two coexist during the snapshot→log transition.
KG_EVENTS_FILE = "kg_events.jsonl"

# Flat until a directory would exceed this many files, then shard by id buckets.
_FLAT_MAX = int(os.environ.get("THREAD_ARCHIVE_SHARD_FLAT_MAX", "16384"))
_BUCKET = 256  # children per shard level

# Cap on simultaneously-open per-thread append handles (LRU-evicted). A bulk import
# usually touches one thread at a time, so the working set is tiny; the cap only
# bounds a pathological fan-out.
_MAX_OPEN_HANDLES = 256
_handles: "OrderedDict[str, TextIO]" = OrderedDict()


# ── config ──────────────────────────────────────────────────────────────────
def log_dir() -> Path:
    """The JSONL truth directory for this archive instance (``<home>/truth``)."""
    from ..config import resolve_paths

    return resolve_paths().truth_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── manifest (shard depth + last-checkpoint watermark) ───────────────────────
def _manifest_path(d: Path) -> Path:
    return d / "manifest.json"


def _read_manifest(d: Path) -> dict:
    p = _manifest_path(d)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover — corrupt/missing → defaults
            pass
    return {"version": 1, "shard_depth": 0, "last_checkpoint_at": None}


def _write_manifest(d: Path, m: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = _manifest_path(d).with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(m, indent=2))
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename makes it visible
    os.replace(tmp, _manifest_path(d))


def _shard_depth(d: Path) -> int:
    return int(_read_manifest(d).get("shard_depth", 0))


# ── per-thread file paths (sharded by recorded depth) ────────────────────────
def _thread_relpath(thread_id: int, depth: int) -> Path:
    """Relative path of a thread's file at ``depth``: flat (depth 0), else nested
    id-bucket dirs (``<id%256>/...``). Deterministic — readers and writers compute
    the same location from the manifest's depth."""
    parts: list[str] = []
    x = int(thread_id)
    for _ in range(depth):
        parts.append(f"{x % _BUCKET:02x}")
        x //= _BUCKET
    return Path(THREADS_SUBDIR, *parts, f"{int(thread_id)}.jsonl")


def _thread_file(d: Path, thread_id: int, depth: int) -> Path:
    return d / _thread_relpath(thread_id, depth)


def _max_dir_occupancy(n_threads: int, depth: int) -> int:
    """Worst-case files in a single directory for ``n_threads`` spread over ``depth``
    bucket levels (256 buckets/level, roughly uniform by id)."""
    return -(-n_threads // (_BUCKET ** depth))  # ceil div


def _depth_for(n_threads: int) -> int:
    depth = 0
    while _max_dir_occupancy(n_threads, depth) > _FLAT_MAX:
        depth += 1
    return depth


# ── serialization ───────────────────────────────────────────────────────────
def _json_default(o: object) -> object:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        return bytes(o).decode("utf-8", "replace")
    return str(o)


def _row_dict(obj: object) -> dict:
    """Every mapped column of an ORM row, by key — captures server-defaulted
    columns (``recorded_at``) populated after flush, so the JSONL is faithful."""
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}  # type: ignore[attr-defined]


def _datetime_cols(model: type) -> set[str]:
    return {c.key for c in model.__table__.columns if isinstance(c.type, DateTime)}  # type: ignore[attr-defined]


def _coerce(model: type, row: dict) -> dict:
    """Reverse :func:`_json_default` for reload: ISO strings → datetime so the
    SQLite DateTime columns round-trip as the live writer wrote them. Keys not
    mapped on the model are dropped, so a truth log written under an older schema
    (columns since removed) still reloads cleanly into the current projection."""
    valid = {c.key for c in model.__table__.columns}
    row = {k: v for k, v in row.items() if k in valid}
    for key in _datetime_cols(model):
        val = row.get(key)
        if isinstance(val, str):
            row[key] = datetime.fromisoformat(val)
    return row


# ── append handles (LRU, one per open thread file) ───────────────────────────
def _repair_torn_tail(path: Path) -> None:
    """Newline-terminate a file whose last append was torn (a crash mid-write).

    Without this, the next append glues its record onto the torn fragment and the
    combined line is unparseable — the torn fragment *consumes a valid event*, and
    reindex skips both. Adding the newline first isolates the fragment as its own
    (already unrecoverable) line so every later record stays intact. Same guard
    :func:`_merge_file_into` applies at its boundary."""
    try:
        if path.stat().st_size == 0:
            return
    except FileNotFoundError:
        return
    with open(path, "rb+") as fh:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) != b"\n":
            fh.write(b"\n")
            fh.flush()
            os.fsync(fh.fileno())
            logger.warning("truth: repaired torn tail (newline-terminated) %s", path)


def _handle(path: Path) -> TextIO:
    key = str(path)
    fh = _handles.get(key)
    if fh is not None and not fh.closed:
        _handles.move_to_end(key)
        return fh
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_torn_tail(path)  # never append onto a torn fragment
    fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — long-lived, closed in reset()
    _handles[key] = fh
    _handles.move_to_end(key)
    while len(_handles) > _MAX_OPEN_HANDLES:
        _, old = _handles.popitem(last=False)
        try:
            old.close()
        except (OSError, ValueError):  # pragma: no cover
            pass
    return fh


def _append_line(path: Path, rec: dict) -> None:
    fh = _handle(path)
    fh.write(json.dumps(rec, default=_json_default, ensure_ascii=False))
    fh.write("\n")
    fh.flush()


def _fsync_handle(path: Path) -> None:
    """fsync a cached append handle so its flushed lines are durable on disk.

    Callers batch: write every line of a logical unit via :func:`_append_line`
    (flush-only), then fsync each touched file once — one fsync per file per
    commit, not per line, keeps bulk imports fast while closing the power-loss
    window between the OS page cache and the platter."""
    fh = _handles.get(str(path))
    if fh is not None and not fh.closed:
        os.fsync(fh.fileno())


def reset_handles() -> None:
    """Close cached append handles (tests / shutdown)."""
    for fh in _handles.values():
        try:
            fh.close()
        except (OSError, ValueError):  # pragma: no cover — already closed / broken handle
            pass
    _handles.clear()


# ── staged writes (durable per-thread append BEFORE the SQLite commit) ───────
# "JSONL is truth" requires the invariant **JSONL ⊇ SQLite**: anything the
# projection has must already be in the truth, never the reverse. A write doesn't
# touch the file directly — it stages (kind, thread_id, row) on the session, and a
# global ``before_commit`` listener flushes each staged record to its thread file
# *before* the COMMIT. A raise there aborts the commit (fail fast — never commit a
# projection we couldn't make durable as truth). If the COMMIT then fails after the
# flush, the truth holds a row SQLite lacks — the safe direction: reindex rebuilds
# it, and dedup_key collapses any re-import. ``after_rollback`` discards the buffer.
_PENDING = "_jsonl_pending"


def _stage(session: Session, kind: str, thread_id: int, row: dict) -> None:
    session.info.setdefault(_PENDING, []).append((kind, int(thread_id), row))


def append_event_row(session: Session, event_row: object) -> None:
    """Stage an event for its thread's truth file. Written only if the session commits."""
    _stage(session, "event", event_row.thread_id, _row_dict(event_row))  # type: ignore[attr-defined]


def record_thread(session: Session, thread: object) -> None:
    """Stage a thread's metadata record for its truth file (written on commit).

    The metadata-write seam: called at thread creation so each thread file opens
    with its ``{"type": "thread", ...}`` record. A later metadata change can re-stage
    (latest wins on reindex); :func:`checkpoint` is the backstop for updates."""
    _stage(session, "thread", thread.id, _row_dict(thread))  # type: ignore[attr-defined]


def unstage_thread(session: Session, thread_id: int) -> None:
    """Discard any staged truth rows for ``thread_id`` (its metadata record and any
    events) from the session's pending buffer — the complement of the staging seam
    for the discard-an-empty-thread path. A thread row deleted before its commit
    must ALSO drop its staged record: otherwise the drain still writes a
    ``threads/<id>.jsonl`` the projection no longer holds, ``verify`` drifts, and
    the next reindex resurrects the thread as an empty ghost. Cross-thread rows
    (kind ``kg_event``) are untouched."""
    pending = session.info.get(_PENDING)
    if not pending:
        return
    tid = int(thread_id)
    session.info[_PENDING] = [
        (kind, t, row) for kind, t, row in pending
        if not (t == tid and kind in ("thread", "event"))
    ]


def append_kg_event(session: Session, kg_event: object) -> None:
    """Stage a curatorial event for the append-only ``kg_events.jsonl`` truth log.

    The knowledge-layer write seam (mirrors :func:`append_event_row` for the
    conversation log): a librarian write flushes the ``KgEvent`` (so ``id`` /
    ``recorded_at`` are populated), stages it here, and the row is appended to the
    single ``kg_events.jsonl`` file before the COMMIT it belongs to — keeping the
    JSONL ⊇ SQLite invariant for curation. ``thread_id`` is irrelevant for the
    cross-thread log (it routes to one file, not a per-thread file), so pass 0."""
    _stage(session, "kg_event", 0, _row_dict(kg_event))


def write_events(session: Session, events: list[Event]) -> list[Event]:
    """Insert events into SQLite *and* stage them for their threads' truth files.

    The single seam so the projection and the truth never diverge: each row is
    flushed to its thread file before the COMMIT it belongs to. ``flush`` populates
    the autoincrement id and the server-default ``recorded_at`` before the row dict
    is snapshotted, so the truth line is faithful. The caller owns the commit."""
    session.add_all(events)
    session.flush()
    for ev in events:
        append_event_row(session, ev)
    return events


@event.listens_for(Session, "before_commit")
def _drain_before_commit(session: Session) -> None:
    # Flush staged truth rows to their thread files *before* the COMMIT, so a row can
    # never land in SQLite without already being in the truth (a raise here aborts
    # the commit). pop() so a second commit on the same session doesn't re-append.
    # Each touched file is fsynced once after its lines are written: the COMMIT that
    # follows is itself fsynced by SQLite, so without this the projection could
    # survive a power loss that the truth doesn't — the one direction the invariant
    # forbids.
    #
    # The drain is all-or-nothing per commit: each file's pre-drain size is recorded
    # and a failure partway (write error, unserializable row, failed fsync) truncates
    # every touched file back before re-raising. Without the rollback, records 1…N−1
    # of a failed batch would stay in the truth while SQLite rolls back — a later
    # reindex would resurrect the partial batch, and a retried import would re-append
    # the same content under fresh ids (the dedup check reads the rolled-back SQLite),
    # doubling events. A power loss mid-drain can still leave a partial batch — that
    # window needs transaction framing in the record format; the torn-tail repair
    # keeps such a file appendable.
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    d = log_dir()
    depth = _shard_depth(d)
    staged: list[tuple[Path, dict]] = []
    for kind, thread_id, row in pending:
        path = d / KG_EVENTS_FILE if kind == "kg_event" else _thread_file(d, thread_id, depth)
        staged.append((path, {"type": kind, **row}))
    baselines: dict[Path, int | None] = {}  # pre-drain size; None = file didn't exist
    try:
        for path, rec in staged:
            if path not in baselines:
                existed = path.exists()
                fh = _handle(path)  # may create the file, and may newline-repair a torn tail
                fh.flush()
                # Post-repair size, so a rollback keeps the repair in place.
                baselines[path] = path.stat().st_size if existed else None
            _append_line(path, rec)
        for path in baselines:
            _fsync_handle(path)
    except BaseException:
        for path, size in baselines.items():
            try:
                fh = _handles.pop(str(path), None)
                if fh is not None and not fh.closed:
                    fh.close()  # drop any buffered partial write with the handle
                if size is None:
                    path.unlink(missing_ok=True)  # we created it — no ghost thread file
                else:
                    with open(path, "rb+") as rb:
                        rb.truncate(size)
            except OSError:  # pragma: no cover — rollback is best-effort
                logger.exception("truth: could not roll back partial append to %s", path)
        raise


@event.listens_for(Session, "after_rollback")
def _discard_on_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


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
        for t in changed:
            path = _thread_file(d, t.id, depth)
            _append_line(path, {"type": "thread", **_row_dict(t)})
            touched.add(path)
            n += 1
    for path in touched:
        _fsync_handle(path)
    return n


def checkpoint(*, snapshots: bool = True) -> dict:
    """Persist the cross-thread overlays + any thread-metadata updates, and keep the
    shard layout balanced. Per-thread event/metadata records are written inline on
    commit; this is the cadence call (and pre-backup call) that makes the directory
    a complete restore set.

    ``snapshots=False`` runs **maintenance only** — the manifest watermark, the
    thread-metadata-update backstop, and shard rebalance — and skips the full
    rewrite of the cross-thread overlay files (``thread_links`` / ``topic_messages``).
    That rewrite is O(graph size) and the live-ingest path never changes those tables
    (conversation import only appends events; the overlays are the migration seed plus
    the append-only ``kg_events`` log), so the watcher uses the cheap maintenance form
    on its cadence instead of rewriting tens of MB every few seconds. The full form
    (overlays included) is for explicit pre-backup / pre-reindex / post-curation use."""
    d = log_dir()
    (d / THREADS_SUBDIR).mkdir(parents=True, exist_ok=True)
    m = _read_manifest(d)
    counts: dict = (
        {name: _write_snapshot(d, name, model) for name, model in _CROSS_THREAD.items()}
        if snapshots
        else {}
    )
    if snapshots:
        # Source-import watermarks: operational state, snapshotted so a reindex of a
        # lost/deleted index (where there is no previous index to carry them from)
        # still restores cursors instead of adopting active sources at EOF. A stale
        # snapshot is safe: import resumes from the older watermark and dedup_key
        # collapses the overlap.
        counts["import_state"] = _write_snapshot(d, "import_state", ImportState)
    # Rebalance BEFORE the thread-metadata backstop, so the backstop appends at the
    # post-rebalance depth and can never manufacture a flat twin of a just-moved file.
    depth = _maybe_rebalance(d, int(m.get("shard_depth", 0)))
    # Re-read the manifest: a concurrent sweep (ours skips when the rebalance lock is
    # held) may have advanced shard_depth — never write a stale depth back over it.
    m = _read_manifest(d)
    depth = max(depth, int(m.get("shard_depth", 0)))
    m["shard_depth"] = depth
    counts["threads_updated"] = _checkpoint_changed_threads(d, depth, m.get("last_checkpoint_at"))
    m["last_checkpoint_at"] = _now_iso()
    _write_manifest(d, m)
    logger.info("jsonl_log checkpoint(snapshots=%s): %s", snapshots, counts)
    return counts


# ── adaptive rebalance (flat → sharded when a directory gets large) ──────────
# Serializes the move/merge sweep across processes (the watcher's maintenance pass
# vs. the daily backup's checkpoint): flock, non-blocking — the loser skips, and
# whichever checkpoint runs next finishes the job. Deliberately distinct from the
# reindex lock: the watcher holds THAT one shared around its whole ingest pass, so
# an exclusive acquire on it here would self-deadlock.
REBALANCE_LOCK_FILE = ".rebalance.lock"


def _rebalance_lock_path() -> Path:
    from ..config import resolve_paths

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
                m["shard_depth"] = target
                _write_manifest(d, m)  # durable BEFORE any file moves (see docstring)
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
            reset_handles()  # our own cached appenders must not span the sweep
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


# ── integrity scan (the primitive behind `archive verify`) ───────────────────
def scan_truth_counts() -> dict:
    """Count thread files + event lines across the truth directory, tallying any
    JSON parse errors. The integrity primitive behind ``archive verify``: a clean
    archive has these match the SQLite projection's thread/event counts (the
    JSONL ⊇ SQLite invariant) with zero parse errors. Each ``threads/<id>.jsonl``
    is one thread (so file count = thread count, matching how reindex loads them)."""
    d = log_dir()
    threads_dir = d / THREADS_SUBDIR
    n_threads = n_events = parse_errors = 0
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            n_threads += 1
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        parse_errors += 1
                        continue
                    if rec.get("type", "event") == "event":
                        n_events += 1
    return {"threads": n_threads, "events": n_events, "parse_errors": parse_errors}


# ── reindex (rebuild the SQLite projection from the JSONL truth) ─────────────
def _iter_jsonl(path: Path, *, errors: list[tuple[str, int]] | None = None):
    """Yield parsed records from a JSONL file, skipping unparseable lines.

    A torn line (a crash mid-append) must not kill :func:`reindex` — the recovery
    primitive has to recover everything parseable, with the same tolerance
    ``archive verify`` (:func:`scan_truth_counts`) already has. Every skipped line
    is logged, and recorded on ``errors`` as ``(path, lineno)`` when given, so
    reindex can report the count instead of silently dropping."""
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                logger.warning("truth: skipping unparseable line %s:%d", path, lineno)
                if errors is not None:
                    errors.append((str(path), lineno))


def _load_table(
    model: type, path: Path, engine, batch: int = 5000,
    *, errors: list[tuple[str, int]] | None = None,
) -> int:
    """Bulk-load a snapshot file (one row per line) into its table."""
    table = model.__table__  # type: ignore[attr-defined]
    total = 0
    buf: list[dict] = []

    def _flush() -> None:
        nonlocal total
        if not buf:
            return
        with engine.begin() as conn:
            conn.execute(insert(table).prefix_with("OR REPLACE"), buf)
        total += len(buf)
        buf.clear()

    for row in _iter_jsonl(path, errors=errors):
        buf.append(_coerce(model, row))
        if len(buf) >= batch:
            _flush()
    _flush()
    return total


def _load_thread_files(
    d: Path, engine, batch: int = 5000,
    *, errors: list[tuple[str, int]] | None = None,
) -> tuple[int, int]:
    """Load every ``threads/**/<id>.jsonl`` into the threads + events tables.

    Per file: the last ``type:thread`` record is the metadata (latest wins), every
    ``type:event`` record is an event. A file with events but no thread record (a
    crash between an import commit and its checkpoint) gets a synthesized minimal
    thread so its events aren't dropped. FK enforcement is off on the loader, so the
    interleaved thread/event inserts need no ordering."""
    threads_dir = d / THREADS_SUBDIR
    thread_buf: list[dict] = []
    event_buf: list[dict] = []
    nt = ne = 0

    def _flush() -> None:
        nonlocal nt, ne
        if thread_buf:
            with engine.begin() as conn:
                conn.execute(insert(Thread.__table__).prefix_with("OR REPLACE"), thread_buf)
            nt += len(thread_buf)
            thread_buf.clear()
        if event_buf:
            with engine.begin() as conn:
                conn.execute(insert(Event.__table__).prefix_with("OR REPLACE"), event_buf)
            ne += len(event_buf)
            event_buf.clear()

    if threads_dir.exists():
        for path in sorted(threads_dir.rglob("*.jsonl")):
            last_thread: dict | None = None
            events: list[dict] = []
            for rec in _iter_jsonl(path, errors=errors):
                kind = rec.pop("type", "event")
                if kind == "thread":
                    last_thread = rec
                else:
                    events.append(rec)
            if last_thread is None:
                try:
                    tid = int(path.stem)
                except ValueError:  # pragma: no cover
                    continue
                last_thread = {"id": tid, "name": f"thread:{tid}"}
                logger.warning("reindex: %s had no thread record — synthesized minimal", path.name)
            thread_buf.append(_coerce(Thread, last_thread))
            for ev in events:
                event_buf.append(_coerce(Event, ev))
            if len(thread_buf) >= batch or len(event_buf) >= batch:
                _flush()
    _flush()
    return nt, ne


def _carry_import_state(index_path: Path, engine) -> int:
    """Carry the source-import watermarks from the previous index into the rebuild.

    ``import_state`` is operational state, not truth — the JSONL doesn't contain it,
    so a plain rebuild would wipe every source cursor. The next poll would then
    re-adopt each event-bearing source at its current EOF (``adopt_if_unwatermarked``),
    permanently skipping any source lines appended since its last import — reindexing
    with live sessions writing would silently lose their tails. The previous live
    index is the freshest copy of the cursors, so it overlays the (possibly stale)
    ``import_state.jsonl`` snapshot seed loaded before this. Rows pointing at threads
    the truth no longer holds are pruned. Returns rows carried."""
    if not index_path.exists():
        return 0
    build_cols = {c.key for c in ImportState.__table__.columns}
    try:
        src = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover — unreadable old index
        return 0
    try:
        try:
            cur = src.execute("SELECT * FROM import_state")
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):  # pragma: no cover — unreadable old index
                logger.warning("reindex: could not read import_state from old index: %s", e)
            return 0
        src_cols = [d[0] for d in cur.description]
        keep = [i for i, c in enumerate(src_cols) if c in build_cols]
        cols = [src_cols[i] for i in keep]
        rows = [tuple(r[i] for i in keep) for r in cur.fetchall()]
    finally:
        src.close()
    if rows:
        stmt = (
            f"INSERT OR REPLACE INTO import_state ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})"
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(stmt, rows)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM import_state WHERE thread_id IS NOT NULL "
            "AND thread_id NOT IN (SELECT id FROM threads)"
        )
    return len(rows)


def _replay_kg_events(
    d: Path, engine, *, errors: list[tuple[str, int]] | None = None,
) -> int:
    """Fold the curatorial event log (``kg_events.jsonl``) onto the knowledge projection.

    Loads the log into the ``kg_events`` table and replays each event in ``id`` order
    through the materializer, mutating ``thread_links`` / ``topic_messages`` on top of
    whatever legacy snapshot seed was already loaded. The materializer is upsert +
    tombstone, so a delta that re-touches a seeded row (or deletes one) reconciles
    cleanly and the replay is idempotent and order-stable. A no-op when the log is
    absent — a pre-librarian (or purely lexical) archive simply has no curation to fold.
    Runs through an ORM ``Session`` so the fold can use the materializer, but it never
    *stages* truth (only :func:`append_kg_event` does), so the before-commit drain is a
    no-op here and the rebuild can't re-write the log it is reading."""
    from ..knowledge.materialize import apply_event

    rows = list(_iter_jsonl(d / KG_EVENTS_FILE, errors=errors))
    for r in rows:
        r.pop("type", None)
    rows.sort(key=lambda r: r.get("id") or 0)
    if not rows:
        return 0
    coerced = [_coerce(KgEvent, r) for r in rows]
    with engine.begin() as conn:
        conn.execute(insert(KgEvent.__table__).prefix_with("OR REPLACE"), coerced)
    with Session(engine) as s:
        for r in coerced:
            apply_event(s, KgEvent(**r))
        s.commit()
    return len(rows)


# ── reindex quiesce lock (shared with the watcher's ingest pass) ─────────────
# flock on <home>/.reindex.lock: the watcher holds it SHARED for the duration of
# each ingest pass; reindex holds it EXCLUSIVE across build+swap. So a reindex
# waits out an in-flight pass, and no event can land in the truth mid-rebuild and
# silently miss the new index. flock auto-releases on process death — a killed
# holder can never wedge the other side.
REINDEX_LOCK_FILE = ".reindex.lock"


def _reindex_lock_path() -> Path:
    from ..config import resolve_paths

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
        yield True
    finally:
        os.close(fd)  # closing the fd releases the flock


@contextmanager
def shared_ingest_lock() -> Generator[None, None, None]:
    """Hold the reindex lock *shared*, blocking — for one-shot writers.

    The watcher uses the non-blocking :func:`try_shared_ingest_lock` (it can just
    skip a pass); a one-shot writer — a CLI import, a librarian mutation — has no
    later pass to retry on, so it waits out an in-flight reindex instead. Every
    cross-process writer must hold this (or the try- variant) around its truth
    append **and** the SQLite commit: an unlocked write can append truth after the
    rebuild's read point and commit into the database inode the swap replaces —
    present in the truth, silently absent from the new index."""
    path = _reindex_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
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


def _fold_wal(path: Path) -> None:
    """Fold any leftover ``-wal`` into the main file and drop the sidecars, so a
    rename moves one complete, self-contained database."""
    if Path(f"{path}-wal").exists():
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _unlink_build(tmp_path: Path) -> None:
    for p in (tmp_path, Path(f"{tmp_path}-wal"), Path(f"{tmp_path}-shm")):
        p.unlink(missing_ok=True)


def reindex(*, vectors: bool = False) -> dict:
    """Rebuild the SQLite store from the JSONL truth directory — build-and-swap.

    The recovery primitive: build a complete new index (schema, per-thread files,
    cross-thread snapshots, kg-event replay, FTS, optionally vectors) in
    ``index.db.rebuild`` next to the live file, then ``os.replace`` it over
    ``index.db``. Atomic against a crash: a reindex killed at any point leaves the
    old index fully intact and the build file is discarded; only the rename — atomic
    on the same filesystem — publishes the new one. Ingest is quiesced for the
    duration via the reindex flock (the watcher skips passes while it's held), so no
    event lands in the truth mid-build and silently misses the new index. In-process
    readers reconnect on the next ``get_engine()`` call (the live engine's pools are
    disposed before the swap); *cross-process* readers keep serving the old inode
    until they reconnect or restart — the accepted stale-read window (see
    docs/plans/reindex-atomic-swap.md for the follow-up).

    Bulk loading targets the build file through a **FK-OFF Core** loader so
    dependency-agnostic inserts need no ordering and the conversation truth-log
    listeners never fire; the kg-event replay runs through a Session on that same
    engine but stages nothing, so it likewise can't re-write the truth it reads.
    Loads are **INSERT OR REPLACE** (last-wins) and unparseable truth lines are
    skipped and counted (``parse_errors``) rather than aborting — a torn last line
    from a crash mid-append can't block recovery. The JSONL is authoritative and
    replayed as-is — including any dangling reference; integrity was the writer's
    job."""
    d = log_dir()

    engine = get_engine()
    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        raise RuntimeError("reindex needs a file-backed index (build-and-swap)")
    index_path = Path(db_path)

    # Pre-flight: the build needs room for a second copy of the index.
    if index_path.exists():
        free = shutil.disk_usage(index_path.parent).free
        need = int(index_path.stat().st_size * 1.2)
        if free < need:
            raise RuntimeError(
                f"reindex: {free / 1e9:.1f} GB free < {need / 1e9:.1f} GB needed "
                "for the temp build — free disk space first"
            )

    tmp_path = index_path.with_name(index_path.name + ".rebuild")
    counts: dict = {}
    parse_errors: list[tuple[str, int]] = []

    with _hold_reindex_lock():
        _unlink_build(tmp_path)  # a dead prior build is stale — start clean
        loader = build_engine(f"sqlite:///{tmp_path}", enforce_fk=False)
        try:
            init_db(loader)
            counts["threads"], counts["events"] = _load_thread_files(d, loader, errors=parse_errors)
            for name, model in _CROSS_THREAD.items():
                counts[name] = _load_table(model, d / f"{name}.jsonl", loader, errors=parse_errors)
            # Source-import watermarks: seed from the checkpoint snapshot, then
            # overlay the previous live index's fresher rows (see _carry_import_state).
            _load_table(ImportState, d / "import_state.jsonl", loader, errors=parse_errors)
            counts["import_state"] = _carry_import_state(index_path, loader)
            counts["kg_events"] = _replay_kg_events(d, loader, errors=parse_errors)

            # FTS + vectors resolve their engine via get_engine(); point them at
            # the build for the block.
            with use_engine(loader):
                from ..retrieval.fts import rebuild_fts

                counts["fts"] = rebuild_fts()
                if vectors:
                    # Restore the durable vector cache (the hours-long embed runs
                    # once, ever), embed only genuinely-new events, then refresh the
                    # sidecar. Degrades to 0 without the [embeddings] extra — the
                    # store stays lexical-only.
                    from ..retrieval import vectors as _vec

                    counts["vectors_restored"] = _vec.load_vectors_sidecar(d)
                    counts["vectors_embedded"] = _vec.index_events_local(rebuild=False)
                    counts["vectors_cached"] = _vec.save_vectors_sidecar(d)
        except BaseException:
            loader.dispose()
            _unlink_build(tmp_path)  # the old index was never touched
            raise
        loader.dispose()
        _fold_wal(tmp_path)

        # Publish: dispose the live engine's pools first (its connections point at
        # the file being replaced), atomically rename the build over index.db, then
        # drop the old sidecars — a stale -wal must never be replayed into the new
        # database. The next get_engine() connection opens the new file.
        engine.dispose()
        os.replace(tmp_path, index_path)
        for suffix in ("-wal", "-shm"):
            Path(f"{index_path}{suffix}").unlink(missing_ok=True)

    counts["parse_errors"] = len(parse_errors)

    # The topic graph caches a projection per engine; drop it so the next read
    # rebuilds over the freshly-loaded thread_links.
    from ..knowledge import reset_cache as _reset_kg

    _reset_kg()

    logger.info("jsonl_log reindex: %s", counts)
    return counts


# ── truth emit (the single per-thread file writer; no-drift seam) ────────────
def emit_thread_file(d: Path, thread_id: int, depth: int, thread_record, event_records) -> int:
    """Atomically (re)write one ``threads/<id>.jsonl``: an optional ``type:thread``
    record then the ``type:event`` records, in order. The one place the on-disk
    per-thread format is produced (used by the store re-emit), so the layout can't
    drift. Returns the event count written."""
    path = _thread_file(d, thread_id, depth)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    n_ev = 0
    with open(tmp, "w", encoding="utf-8") as fh:
        if thread_record is not None:
            fh.write(json.dumps({"type": "thread", **thread_record}, default=_json_default, ensure_ascii=False))
            fh.write("\n")
        for ev in event_records:
            fh.write(json.dumps({"type": "event", **ev}, default=_json_default, ensure_ascii=False))
            fh.write("\n")
            n_ev += 1
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename makes it visible
    os.replace(tmp, path)
    return n_ev


def rebuild_truth_from_store() -> dict:
    """Re-emit the entire per-thread truth from the current SQLite store.

    The inverse of :func:`reindex`: for every thread, (re)write ``threads/<id>.jsonl``
    as its metadata record + ordered events; rewrite the cross-thread snapshots; pick
    the shard depth for the thread count; set the manifest. Used once to migrate an
    older monolithic ``events.jsonl`` into per-thread files (the old monolith files,
    if present, are removed). Idempotent — safe to re-run."""
    d = log_dir()
    (d / THREADS_SUBDIR).mkdir(parents=True, exist_ok=True)

    with get_session() as s:
        threads = s.execute(select(Thread).order_by(Thread.id)).scalars().all()
        depth = _depth_for(len(threads))
        nt = ne = 0
        emitted: set[int] = set()
        for t in threads:  # outer list is materialized, so the inner event stream is the only cursor
            ev_rows = (_row_dict(ev) for ev in s.execute(
                select(Event).where(Event.thread_id == t.id).order_by(Event.id)
            ).scalars())
            ne += emit_thread_file(d, t.id, depth, _row_dict(t), ev_rows)
            emitted.add(t.id)
            nt += 1

    for name, model in _CROSS_THREAD.items():
        _write_snapshot(d, name, model)
    _write_snapshot(d, "import_state", ImportState)  # cursors survive the re-emit too

    _write_manifest(d, {"version": 1, "shard_depth": depth, "last_checkpoint_at": _now_iso()})

    # Remove stale copies of re-emitted threads left at another shard depth. Their
    # content was just fully re-emitted at ``depth``, so an old-layout copy is pure
    # duplication — and on a later reindex a stale duplicate line would shadow the
    # fresh (possibly repaired) row for the same event id. Files whose ids the store
    # does NOT hold are left untouched.
    for path in list((d / THREADS_SUBDIR).rglob("*.jsonl")):
        try:
            tid = int(path.stem)
        except ValueError:  # pragma: no cover — stray file
            continue
        if tid in emitted and path != _thread_file(d, tid, depth):
            path.unlink()

    # Drop the old monolithic files this format replaces.
    for old in ("events.jsonl", "threads.jsonl"):
        p = d / old
        if p.exists():
            p.unlink()

    result = {"threads": nt, "events": ne, "shard_depth": depth}
    logger.info("jsonl_log rebuild_truth_from_store: %s", result)
    return result
