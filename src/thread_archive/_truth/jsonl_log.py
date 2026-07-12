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
SQLite issues; neither uses ``F_FULLFSYNC``). Each append batch is framed by a
drain-intent undo journal, so a power loss mid-batch is rolled back to the
pre-batch baselines on the next write (see the drain-intent section).
:func:`reindex` is the recovery
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
import uuid
from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from sqlalchemy import DateTime, event, insert, select
from sqlalchemy.orm import Session

from .._store import (
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
    from .._config import resolve_paths

    return resolve_paths().truth_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(d: Path) -> None:
    """fsync a directory so a just-created (or just-renamed-in) file's dirent is
    durable. Without it, a power loss can drop a fully-fsynced new file from the
    directory while the SQLite commit that depended on it survives — index ⊃ truth,
    the one direction the invariant forbids."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:  # pragma: no cover — directory vanished / unreadable
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover — fs doesn't support dir fsync
        pass
    finally:
        os.close(fd)


# ── manifest (shard depth + last-checkpoint watermark) ───────────────────────

# The on-disk truth format version, recorded as ``version`` in manifest.json and
# specified in docs/format.md. Bump only for a change an existing reader would
# misinterpret (record shapes, file layout, sharding semantics) — additive
# optional fields don't count. Code that meets a *newer* version must refuse
# the archive rather than guess (see :func:`_read_manifest`).
TRUTH_FORMAT_VERSION = 1


class TruthFormatError(RuntimeError):
    """The truth directory declares a format version newer than this code reads."""


def _manifest_path(d: Path) -> Path:
    return d / "manifest.json"


def _infer_shard_depth(d: Path) -> int:
    """Infer the shard depth from the directory shape — the fallback when the
    manifest is unreadable. A sharded layout is unmistakable: ``threads/`` contains
    two-hex-digit bucket directories (which may themselves contain buckets). Walking
    the first bucket chain recovers the depth, so a corrupt or deleted manifest on a
    sharded archive can never silently reset writers to the flat layout (which would
    grow flat twins of sharded files and let stale metadata shadow fresh records on
    reindex)."""
    depth = 0
    cur = d / THREADS_SUBDIR
    while depth < 4:  # bound: depths beyond 2 don't exist, but never loop unbounded
        try:
            bucket = next(
                (
                    e for e in os.scandir(cur)
                    if e.is_dir() and len(e.name) == 2
                    and all(c in "0123456789abcdef" for c in e.name)
                ),
                None,
            )
        except OSError:
            break
        if bucket is None:
            break
        depth += 1
        cur = Path(bucket.path)
    return depth


def _read_manifest(d: Path) -> dict:
    p = _manifest_path(d)
    if p.exists():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt manifest: fall through to defaults, but NEVER default the
            # shard depth on a sharded archive — infer it from the layout below.
            logger.error("truth: manifest.json unreadable — inferring shard depth")
        else:
            declared = m.get("version", TRUTH_FORMAT_VERSION)
            if isinstance(declared, int) and declared > TRUTH_FORMAT_VERSION:
                # A newer layout may have changed sharding or record semantics;
                # reading it with old assumptions risks writing flat twins or
                # shadowing fresh records — refuse instead of guessing.
                raise TruthFormatError(
                    f"truth directory {d} declares format version {declared}, but this "
                    f"code reads version {TRUTH_FORMAT_VERSION}. Upgrade thread-archive "
                    "to a release that supports it."
                )
            return m
    depth = _infer_shard_depth(d)
    if depth:
        logger.warning(
            "truth: no readable manifest; inferred shard_depth=%d from layout", depth
        )
    return {"version": TRUTH_FORMAT_VERSION, "shard_depth": depth, "last_checkpoint_at": None}


def _write_manifest(d: Path, m: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    # Per-process tmp name: manifest writers (watcher maintenance, a manual
    # checkpoint, a backup) aren't otherwise serialized, and two processes
    # sharing one tmp path would interleave writes and publish a torn manifest.
    tmp = _manifest_path(d).with_name(f"manifest.json.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(m, indent=2))
            fh.flush()
            os.fsync(fh.fileno())  # durable before the rename makes it visible
        os.replace(tmp, _manifest_path(d))
    finally:
        tmp.unlink(missing_ok=True)  # no-op after the rename; clears a failed write
    _fsync_dir(d)  # the rename itself must survive power loss


# Serializes manifest read-modify-write across processes. The manifest carries
# keys owned by different writers (shard_depth: checkpoint/rebalance; hashes
# baseline: verify), and concurrent whole-file replaces would lose whichever
# writer published first. Leaf lock: mutators run pure, nothing else is
# acquired while it is held, so it can nest inside any other lock safely.
MANIFEST_LOCK_FILE = ".manifest.lock"


def _manifest_lock_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / MANIFEST_LOCK_FILE


def update_manifest(d: Path, mutate) -> dict:
    """Atomically read-modify-write the manifest: ``mutate(m)`` edits the dict in
    place under an exclusive flock, so no concurrent writer's keys are lost to a
    stale read. Returns the manifest as written."""
    path = _manifest_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        m = _read_manifest(d)
        mutate(m)
        _write_manifest(d, m)
        return m
    finally:
        os.close(fd)  # closing the fd releases the flock


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


def _same_inode(fh: TextIO, path: Path) -> bool:
    """True when the cached handle still writes the file at ``path``. A truth
    re-emit (``rebuild_truth_from_store``) atomically replaces thread files, so a
    handle cached across it points at the unlinked old inode — appends through it
    vanish while their SQLite commits survive, the one direction the invariant
    forbids. Two stats per append; the price of never writing to a dead file."""
    try:
        a = os.fstat(fh.fileno())
        b = os.stat(path)
    except (OSError, ValueError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _handle(path: Path) -> TextIO:
    key = str(path)
    fh = _handles.get(key)
    if fh is not None and not fh.closed and _same_inode(fh, path):
        _handles.move_to_end(key)
        return fh
    if fh is not None and not fh.closed:
        try:
            fh.close()  # stale inode — the file was replaced under us
        except (OSError, ValueError):  # pragma: no cover
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_torn_tail(path)  # never append onto a torn fragment
    is_new = not path.exists()
    fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 — long-lived, closed in reset()
    if is_new:
        _fsync_dir(path.parent)  # the new file's dirent must be as durable as its rows
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
    window between the OS page cache and the platter.

    A batch touching more than ``_MAX_OPEN_HANDLES`` files LRU-evicts its early
    handles before this runs; eviction close() flushes to the OS but does not
    fsync, so an evicted file is reopened here and fsynced by fd — the durability
    bar must not quietly drop for bulk batches. An OSError propagates (fail fast,
    same as an fsync failure on a live handle)."""
    fh = _handles.get(str(path))
    if fh is not None and not fh.closed:
        os.fsync(fh.fileno())
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def reset_handles() -> None:
    """Close cached append handles (tests / shutdown)."""
    for fh in _handles.values():
        try:
            fh.close()
        except (OSError, ValueError):  # pragma: no cover — already closed / broken handle
            pass
    _handles.clear()


# ── drain intent (crash framing for append batches) ──────────────────────────
# The all-or-nothing drain rollback handles *process* failure, but a power loss
# mid-batch leaves a partial batch in the truth with nothing to distinguish it
# from committed records. The intent file frames each batch as an undo journal:
# the drain durably records ``{txn, files: [{path, baseline, ids}]}`` BEFORE its
# first data write and empties the file after its last data fsync — both inside
# the truth-write mutex, so a non-empty intent is only ever observable after a
# crash. Recovery runs on every acquisition of the mutex (and at reindex start):
# it rolls the framed files back to their baselines, guarded two ways —
#   * the committed-ids check: any of the batch's freshly-inserted ids present in
#     the live index means the COMMIT landed (it was atomic), so a crash *after*
#     COMMIT can never truncate committed truth (index ⊃ truth is the forbidden
#     direction);
#   * the tail check: everything past a file's baseline must be the framed
#     batch's own records (a torn final fragment — the crash itself — is
#     allowed). Anything else means another writer appended after the crash, and
#     the file is left in place rather than risk cutting foreign records.
DRAIN_INTENT_FILE = ".drain.intent"

# Intent record kinds whose ids are fresh inserts in an index table — the basis
# of the committed-ids check. ``thread`` is deliberately absent: a metadata
# re-stage carries a thread id that pre-exists in the index whether or not the
# batch committed, so it can't witness the commit.
_INTENT_TABLES = {"event": "events", "kg_event": "kg_events"}


def _intent_path() -> Path:
    from .._config import resolve_paths

    return resolve_paths().home / DRAIN_INTENT_FILE


def _write_intent(files: list[dict]) -> None:
    """Durably frame the batch about to be appended: fsynced BEFORE the first
    data write, so a crash mid-batch always leaves the frame recovery needs to
    identify — and remove — the partial batch."""
    p = _intent_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(
            {"txn": uuid.uuid4().hex, "at": _now_iso(), "files": files},
            fh, default=_json_default,
        )
        fh.flush()
        os.fsync(fh.fileno())
    if not existed:
        _fsync_dir(p.parent)  # the frame's dirent must be as durable as the frame


def _clear_intent() -> None:
    """Empty the intent after the batch's last data fsync, still under the write
    mutex — an intent must never be visible outside its batch's lock hold. Not
    fsynced: if a crash resurrects a cleared-but-complete intent, the
    committed-ids check in recovery resolves it correctly."""
    try:
        with open(_intent_path(), "w", encoding="utf-8"):
            pass
    except OSError:  # pragma: no cover — best-effort; recovery re-resolves it
        logger.exception("truth: could not clear drain intent")


def _intent_committed(files: list[dict]) -> bool | None:
    """Whether the framed batch's COMMIT landed. Any of its freshly-inserted ids
    present in the live index means yes (the COMMIT was atomic — all or none).
    ``None`` = cannot tell (no insert ids in the batch, or no readable index),
    which callers treat as committed: keeping possibly-uncommitted records is the
    safe direction (dedup collapse bounds the damage), truncating possibly-
    committed ones is the forbidden one."""
    by_table: dict[str, list[int]] = {}
    for f in files:
        for kind, rid in f.get("ids") or []:
            table = _INTENT_TABLES.get(kind)
            if table is not None and rid is not None:
                by_table.setdefault(table, []).append(int(rid))
    if not by_table:
        return None
    db = get_engine().url.database
    if not db or db == ":memory:":
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        for table, ids in by_table.items():
            probe = ids[:32]  # one committed id proves the whole batch
            try:
                row = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id IN ({','.join('?' * len(probe))}) LIMIT 1",  # noqa: S608 — table from _INTENT_TABLES
                    probe,
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is not None:
                return True
        return False
    finally:
        conn.close()


def _tail_is_intent_only(path: Path, baseline: int, allowed: set) -> bool:
    """True when everything past ``baseline`` is the framed batch's own records.
    An unparseable FINAL fragment — the torn write of the crash itself — is
    allowed; any other content means a writer appended after the crash and a
    truncate would destroy records that aren't the batch's."""
    try:
        with open(path, "rb") as fh:
            fh.seek(baseline)
            chunks = fh.read().split(b"\n")
    except OSError:
        return False
    for i, raw in enumerate(chunks):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            return i == len(chunks) - 1  # torn final fragment only
        if (rec.get("type", "event"), rec.get("id")) not in allowed:
            return False
    return True


def _recover_crashed_drain() -> None:
    """Resolve a leftover drain intent — the mark of a process that died (power
    loss, SIGKILL) mid-batch. Runs under the truth-write mutex on every
    acquisition: a live drain's intent never outlives its lock hold, so anything
    found here is a dead process's. Uncommitted framed files are rolled back to
    their baselines (the crash-durable twin of the drain's in-process rollback);
    a committed or undecidable batch keeps its records. Never raises — recovery
    is a repair pass, and a bug in it must not take writes down."""
    p = _intent_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return
    if not raw.strip():
        return
    try:
        intent = json.loads(raw)
        files = list(intent["files"])
    except (ValueError, KeyError, TypeError):
        # A torn intent write: the crash hit before any data append (the intent
        # is durable before the first write), so the truth is untouched.
        logger.warning("truth: discarding torn drain intent (no data was written)")
        _clear_intent()
        return
    try:
        committed = _intent_committed(files)
        if committed is not False:
            logger.warning(
                "truth: drain intent txn=%s survived a crash but its commit %s — keeping its records",
                intent.get("txn"), "landed" if committed else "cannot be determined",
            )
            _clear_intent()
            return
        d = log_dir()
        rolled_back = kept = 0
        for f in files:
            path = d / f["path"]
            baseline = f.get("baseline")
            allowed = {(k, i) for k, i in f.get("ids") or []}
            try:
                size = path.stat().st_size
            except OSError:
                continue  # never created (crash before its first append) or already gone
            if baseline is not None and size <= baseline:
                continue  # nothing of the batch landed here
            if not _tail_is_intent_only(path, baseline or 0, allowed):
                kept += 1
                logger.error(
                    "truth: crashed drain tail of %s holds records outside its intent — left in place",
                    path,
                )
                continue
            fh = _handles.pop(str(path), None)
            if fh is not None and not fh.closed:
                fh.close()
            if baseline is None:
                path.unlink(missing_ok=True)  # the batch created it — no ghost thread file
                _fsync_dir(path.parent)
            else:
                with open(path, "rb+") as rb:
                    rb.truncate(baseline)
                    os.fsync(rb.fileno())
            rolled_back += 1
        logger.warning(
            "truth: recovered crashed drain txn=%s — %d file(s) rolled back to baseline%s",
            intent.get("txn"), rolled_back,
            f", {kept} left for inspection" if kept else "",
        )
    except Exception:  # pragma: no cover — recovery must never block writes
        logger.exception("truth: crashed-drain recovery failed; intent cleared")
    _clear_intent()


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
    path = _truth_write_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # A leftover intent is a dead process's crashed batch — resolve it before
        # this holder appends (or moves files) on top of the partial tail.
        _recover_crashed_drain()
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


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
    # doubling events. The same guarantee against a *power loss* mid-drain comes from
    # the intent frame (see the drain-intent section): the batch's files, baselines
    # and ids are durable before its first data write, so recovery can roll a partial
    # batch back to the same baselines the in-process rollback uses.
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    d = log_dir()
    with _truth_write_lock():
        # Depth read under the lock, so a rebalance that bumped it between our
        # staging and this drain can't leave us appending at a stale layout.
        depth = _shard_depth(d)
        staged: list[tuple[Path, dict]] = []
        for kind, thread_id, row in pending:
            path = d / KG_EVENTS_FILE if kind == "kg_event" else _thread_file(d, thread_id, depth)
            staged.append((path, {"type": kind, **row}))
        # Baselines (and open handles) for every touched file BEFORE any append,
        # so the intent frame describes the whole batch up front.
        baselines: dict[Path, int | None] = {}  # pre-drain size; None = file didn't exist
        ids_by_path: dict[Path, list] = {}
        for path, rec in staged:
            if path not in baselines:
                existed = path.exists()
                fh = _handle(path)  # may create the file, and may newline-repair a torn tail
                fh.flush()
                # Post-repair size, so a rollback keeps the repair in place.
                baselines[path] = path.stat().st_size if existed else None
            ids_by_path.setdefault(path, []).append([rec["type"], rec.get("id")])
        _write_intent([
            {"path": str(path.relative_to(d)), "baseline": size, "ids": ids_by_path[path]}
            for path, size in baselines.items()
        ])  # durable BEFORE the first data write
        try:
            for path, rec in staged:
                _append_line(path, rec)
            for path in baselines:
                _fsync_handle(path)
        except BaseException:
            # Truncate-to-baseline is safe under the write lock: no other writer
            # can have appended to these files since the baseline was taken.
            # Each undo is fsynced: the intent clear below is not, so an
            # un-durable truncate surviving a power loss alongside a durably
            # cleared intent would resurrect the partial batch with no frame
            # left to roll it back.
            for path, size in baselines.items():
                try:
                    fh = _handles.pop(str(path), None)
                    if fh is not None and not fh.closed:
                        fh.close()  # drop any buffered partial write with the handle
                    if size is None:
                        path.unlink(missing_ok=True)  # we created it — no ghost thread file
                        _fsync_dir(path.parent)
                    else:
                        with open(path, "rb+") as rb:
                            rb.truncate(size)
                            os.fsync(rb.fileno())
                except OSError:  # pragma: no cover — rollback is best-effort
                    logger.exception("truth: could not roll back partial append to %s", path)
            _clear_intent()  # the batch is undone; the frame must not outlive it
            raise
        _clear_intent()  # inside the lock: an intent is never visible outside its batch
        # Remember what this drain appended (per file: baseline → post-drain size)
        # so a COMMIT failure *after* the drain can compensate (see
        # _undo_drain_on_rollback). Cleared on successful commit.
        session.info[_DRAINED] = {
            path: (size, path.stat().st_size) for path, size in baselines.items()
        }


# A successful drain's footprint, kept on the session until its COMMIT lands:
# {path: (baseline_size_or_None, post_drain_size)}. The compensation seam for
# the one atomicity gap the drain leaves open — truth is written and fsynced
# before the SQLite COMMIT, so a COMMIT that then fails (disk full, a
# constraint enforced at the final flush, busy timeout) leaves the batch in the
# truth with no projection row. That is the *safe* direction (JSONL ⊇ SQLite),
# but it isn't atomic: a later reindex materializes the uncommitted batch.
_DRAINED = "_jsonl_drained"


@event.listens_for(Session, "after_commit")
def _forget_drain_on_commit(session: Session) -> None:
    session.info.pop(_DRAINED, None)


@event.listens_for(Session, "after_rollback")
def _discard_on_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


@event.listens_for(Session, "after_transaction_end")
def _compensate_failed_commit(session: Session, transaction) -> None:
    # The root transaction ending with the drain footprint still present means
    # the COMMIT never landed (a successful one pops it in after_commit, which
    # fires first). after_transaction_end — not after_rollback — is the hook
    # that actually fires on every such path: a commit that raises mid-flight
    # ends via the session context manager's close(), which never emits
    # after_rollback.
    if transaction.parent is None and _DRAINED in session.info:
        _undo_drain(session)


def _undo_drain(session: Session) -> None:
    """Best-effort compensation when a COMMIT failed after its drain succeeded:
    truncate each touched truth file back to its pre-drain baseline, so the
    truth doesn't keep a transaction SQLite rejected (which reindex would
    otherwise resurrect, and which — for metadata records — carries no fresh
    event id for recovery to reconcile against).

    Only provably-safe undos run: under the exclusive truth-write lock, a file
    is rolled back only if its current size still equals the drain's post-drain
    size — any other writer's append since (sizes differ) leaves the file
    alone, degrading to the documented JSONL ⊇ SQLite behavior. Failure here is
    logged, never raised: the transaction is already over and the leftover
    truth rows are the benign direction (dedup collapses a re-import)."""
    drained: dict[Path, tuple[int | None, int]] | None = session.info.pop(_DRAINED, None)
    if not drained:
        return
    try:
        with _truth_write_lock():
            for path, (baseline, post_size) in drained.items():
                try:
                    if not path.exists() or path.stat().st_size != post_size:
                        continue  # someone else appended (or repaired) — leave it
                    fh = _handles.pop(str(path), None)
                    if fh is not None and not fh.closed:
                        fh.close()
                    if baseline is None:
                        path.unlink(missing_ok=True)  # the drain created it
                        _fsync_dir(path.parent)
                    else:
                        with open(path, "rb+") as rb:
                            rb.truncate(baseline)
                            os.fsync(rb.fileno())
                except OSError:  # pragma: no cover — compensation is best-effort
                    logger.exception(
                        "truth: could not undo drained batch in %s after rollback", path
                    )
    except OSError:  # pragma: no cover — lock unavailable; leave the safe direction
        logger.exception("truth: post-rollback drain compensation skipped")


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
    thread-metadata-update backstop, the (small) ``import_state`` snapshot, and
    shard rebalance — and skips the full
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
            reset_handles()  # our own cached appenders must not span the sweep
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


# ── integrity scan (the primitive behind `archive verify`) ───────────────────
def scan_truth_counts(
    *, event_id_max: int | None = None, thread_id_max: int | None = None,
    kg_event_id_max: int | None = None, truth_dir: Path | None = None,
) -> dict:
    """Count threads + event lines across the truth directory — the per-thread
    files *and* the curatorial log (``kg_events.jsonl``) — tallying any JSON
    parse errors. The integrity primitive behind ``archive verify``: a clean
    archive has these match the SQLite projection's thread/event/kg-event counts
    (the JSONL ⊇ SQLite invariant) with zero parse errors.

    The truth is append-only, so it legitimately accumulates superseded lines the
    projection collapses: a re-appended line for an id it already holds (a crash
    between a rebalance merge-copy and its unlink), and a same-content twin under
    a fresh id (a re-import after the original's commit was lost — same
    ``dedup_key``). ``events`` counts raw lines; ``events_effective`` counts what
    the projection materializes — distinct on id, then distinct on ``dedup_key``
    (falling back to id when NULL). The index is compared against
    ``events_effective``; the superseded remainder is reported, not drift.

    A thread is counted once per distinct file *stem*, and the collapse runs
    across every file sharing that stem — so a thread present at two shard
    depths (a both-layouts backup mirror, a mid-migration crash) counts as one
    thread and its duplicated lines as superseded, matching what a reindex of
    that directory materializes.

    ``kg_events`` counts the curatorial log's distinct ids (the file is
    append-only, so a crash-merge can legitimately duplicate a line; the
    projection materializes one row per id). Its unparseable lines fold into the
    same ``parse_errors`` tally and torn-tail/interior split as the per-thread
    files — the curation truth deserves the same daily scan the conversation
    truth gets, not a weekly one.

    ``event_id_max`` / ``thread_id_max`` / ``kg_event_id_max`` bound the scan to
    ids at or below a stable watermark, so a verify racing live ingest (truth
    lines land *before* their commit; the index keeps growing while the scan
    reads files) compares the same committed prefix on both sides instead of
    false-alarming.

    ``truth_dir`` scans an explicit directory instead of the live archive's —
    the primitive behind the backup-side check (``archive verify --backup``)."""
    d = truth_dir if truth_dir is not None else log_dir()
    threads_dir = d / THREADS_SUBDIR
    n_events = n_effective = parse_errors = 0
    dup_id_lines = dup_content_lines = 0
    parse_error_sample: list[str] = []
    parse_error_locs: list[tuple[str, int]] = []
    files_by_stem: dict[str, list[Path]] = {}
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            if thread_id_max is not None:
                try:
                    if int(path.stem) > thread_id_max:
                        continue
                except ValueError:  # pragma: no cover — stray file
                    pass
            files_by_stem.setdefault(path.stem, []).append(path)
    for paths in files_by_stem.values():
        seen_ids: set = set()
        seen_keys: set = set()
        for path in paths:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        parse_errors += 1
                        parse_error_locs.append((str(path), lineno))
                        if len(parse_error_sample) < 10:
                            parse_error_sample.append(f"{path}:{lineno}")
                        continue
                    if rec.get("type", "event") != "event":
                        continue
                    ev_id = rec.get("id")
                    if event_id_max is not None and ev_id is not None and ev_id > event_id_max:
                        continue
                    n_events += 1
                    if ev_id in seen_ids:
                        dup_id_lines += 1
                        continue
                    seen_ids.add(ev_id)
                    key = rec.get("dedup_key") or ("id", ev_id)
                    if key in seen_keys:
                        dup_content_lines += 1
                        continue
                    seen_keys.add(key)
                    n_effective += 1
    # The curatorial log: distinct kg-event ids at or below the watermark, its
    # parse errors folded into the same tally (and torn/interior split) so a
    # damaged curation line fails the daily verify, not just the weekly deep one.
    kg_ids: set = set()
    kg_path = d / KG_EVENTS_FILE
    if kg_path.exists():
        with open(kg_path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    parse_error_locs.append((str(kg_path), lineno))
                    if len(parse_error_sample) < 10:
                        parse_error_sample.append(f"{kg_path}:{lineno}")
                    continue
                kg_id = rec.get("id")
                if kg_id is None or (kg_event_id_max is not None and kg_id > kg_event_id_max):
                    continue
                kg_ids.add(int(kg_id))
    # Same split reindex reports: a torn tail (the file's final non-empty line —
    # the residue of a crash mid-append, waiting for `archive repair`) vs. interior
    # damage (a fragment later appends isolated, or corruption of a formerly-good
    # line — repair quarantines it and restores any committed event it shadowed).
    torn, interior = _classify_parse_errors(parse_error_locs)
    return {
        "threads": len(files_by_stem),
        "events": n_events,
        "events_effective": n_effective,
        "kg_events": len(kg_ids),
        "duplicate_id_lines": dup_id_lines,
        "duplicate_content_lines": dup_content_lines,
        "parse_errors": parse_errors,
        "parse_errors_torn_tail": len(torn),
        "parse_errors_interior": len(interior),
        "parse_error_sample": parse_error_sample,
    }


# ── reindex (rebuild the SQLite projection from the JSONL truth) ─────────────
def _final_nonempty_lineno(path: Path) -> int | None:
    """Line number of the file's last non-empty line (None when unreadable/empty).
    The classifier for parse errors: an unparseable FINAL line is the accepted
    crash artifact (a torn last append — the same fragment
    :func:`_repair_torn_tail` newline-isolates); an unparseable line anywhere
    else is interior corruption."""
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if line.strip():
                    last = lineno
    except OSError:
        return None
    return last


def _classify_parse_errors(
    errors: list[tuple[str, int]],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Split recorded parse errors into ``(torn_tails, interior)``. A torn tail —
    the file's final non-empty line — is the expected residue of a crash
    mid-append and never blocks recovery; anything interior means the truth was
    corrupted some other way and must be looked at, not silently dropped."""
    torn: list[tuple[str, int]] = []
    interior: list[tuple[str, int]] = []
    last_by_path: dict[str, int | None] = {}
    for path, lineno in errors:
        if path not in last_by_path:
            last_by_path[path] = _final_nonempty_lineno(Path(path))
        (torn if lineno == last_by_path[path] else interior).append((path, lineno))
    return torn, interior


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


def thread_file_load_order(d: Path) -> list[Path]:
    """Every ``threads/**/*.jsonl`` in the order reindex loads them: path-sorted,
    then any file at its thread's *canonical* (manifest-depth) location moved
    last. Loads are last-wins (OR REPLACE by id; latest thread record), so when a
    thread has files at more than one shard depth — a both-layouts backup mirror,
    a mid-migration crash — the canonical file's records must win: it is the one
    live writers append to, so it is at least as fresh as any stale twin. Plain
    path order would let a stale flat twin load *after* its sharded home
    (``threads/00/…`` sorts before ``threads/12345.jsonl``) and shadow it."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return []
    depth = _shard_depth(d)

    def _canonical(path: Path) -> bool:
        try:
            return path == _thread_file(d, int(path.stem), depth)
        except ValueError:
            return False

    paths = sorted(threads_dir.rglob("*.jsonl"))
    paths.sort(key=_canonical)  # stable: non-canonical first, canonical last
    return paths


def _load_thread_files(
    d: Path, engine, batch: int = 5000,
    *, errors: list[tuple[str, int]] | None = None,
) -> tuple[int, int]:
    """Load every ``threads/**/<id>.jsonl`` into the threads + events tables.

    Per file: the last ``type:thread`` record is the metadata (latest wins), every
    ``type:event`` record is an event. Files load in :func:`thread_file_load_order`
    (canonical-depth file last), so a thread with a stale twin at another shard
    depth materializes the canonical file's records. A file with events but no
    thread record (a crash between an import commit and its checkpoint) gets a
    synthesized minimal thread so its events aren't dropped. FK enforcement is off
    on the loader, so the interleaved thread/event inserts need no ordering."""
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

    have_meta: set[int] = set()  # tids with a real thread record already loaded
    for path in thread_file_load_order(d):
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
            if tid in have_meta:
                # A twin of this thread already supplied its real record; a
                # synthesized stub loading after it would clobber real metadata.
                last_thread = None
            else:
                last_thread = {"id": tid, "name": f"thread:{tid}"}
                logger.warning("reindex: %s had no thread record — synthesized minimal", path.name)
        else:
            try:
                have_meta.add(int(last_thread.get("id")))
            except (TypeError, ValueError):  # pragma: no cover — malformed record
                pass
        if last_thread is not None:
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
    from .._knowledge.materialize import apply_event

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


def _committed_regression(index_path: Path, tmp_path: Path) -> dict | None:
    """Committed records the rebuild would lose, diffed against the current index.

    The gate behind fail-closed publication: every event and kg-event id the
    live index holds must survive into the build — an event may instead survive
    as a same-content twin (same ``(thread_id, dedup_key)`` under another id;
    dedup collapse legitimately re-keys those). Threads are checked by id too:
    an event-less thread lost to a name-conflict ``OR REPLACE`` (or a deleted
    truth file) has no event row to trip the event diff. Anything else missing
    means the truth lost committed content (a damaged line, a deleted file) and
    the swap would make the loss permanent-by-default. Returns ``None`` when
    there is no readable previous index to diff against (a fresh restore has no
    baseline)."""
    if not index_path.exists():
        return None
    conn = sqlite3.connect(tmp_path)
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS old", (str(index_path),))
            lost_threads = conn.execute(
                "SELECT count(*) FROM old.threads o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.threads n WHERE n.id = o.id)"
            ).fetchone()[0]
            thread_sample = [r[0] for r in conn.execute(
                "SELECT o.id FROM old.threads o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.threads n WHERE n.id = o.id) "
                "ORDER BY o.id LIMIT 10"
            ).fetchall()] if lost_threads else []
            lost_events = conn.execute(
                "SELECT count(*) FROM old.events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.events n WHERE n.id = o.id) "
                "AND (o.dedup_key IS NULL OR NOT EXISTS("
                "  SELECT 1 FROM main.events n "
                "  WHERE n.thread_id = o.thread_id AND n.dedup_key = o.dedup_key))"
            ).fetchone()[0]
            sample = [r[0] for r in conn.execute(
                "SELECT o.id FROM old.events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.events n WHERE n.id = o.id) "
                "AND (o.dedup_key IS NULL OR NOT EXISTS("
                "  SELECT 1 FROM main.events n "
                "  WHERE n.thread_id = o.thread_id AND n.dedup_key = o.dedup_key)) "
                "ORDER BY o.id LIMIT 10"
            ).fetchall()] if lost_events else []
            lost_kg = conn.execute(
                "SELECT count(*) FROM old.kg_events o "
                "WHERE NOT EXISTS(SELECT 1 FROM main.kg_events n WHERE n.id = o.id)"
            ).fetchone()[0]
        except sqlite3.Error as e:
            # An unreadable / pre-schema old index is no baseline — the rebuild
            # IS the recovery. Never let the gate itself block it.
            logger.warning("reindex: cannot diff against the previous index (%s)", e)
            return None
    finally:
        conn.close()
    return {
        "events": int(lost_events), "event_sample": sample,
        "kg_events": int(lost_kg),
        "threads": int(lost_threads), "thread_sample": thread_sample,
    }


def _parsed_equal(a: object, b: object) -> bool:
    """True when two stored payload texts encode the same object. Raw-text
    inequality alone must not count — the two stores may serialize one object
    differently across code versions — so candidates are confirmed by parsed
    comparison. An unparseable side is a real disagreement (that copy is
    damaged)."""
    if a == b:
        return True
    try:
        pa = json.loads(a) if isinstance(a, str) else a
        pb = json.loads(b) if isinstance(b, str) else b
    except ValueError:
        return False
    return pa == pb


def _content_divergence(index_path: Path, tmp_path: Path) -> dict | None:
    """Same-id events whose content disagrees between the current index and the
    rebuild — publication *visibility*, deliberately not a gate.

    The truth is authoritative: on publish the rebuild's content wins, and the
    projection disagreeing must not block the swap. But nothing mutates a
    payload after commit, so a same-id disagreement means one copy is damaged —
    and for an event with no hash-tailed ``dedup_key`` the live index row can be
    the last good copy of a truth line rotted in place. Cross-store parity
    (``verify --hashes``) can only see the disagreement while both copies still
    exist; the moment the swap lands they agree and the rot is laundered.
    Publication is therefore the last observable moment of the overwrite: count
    it and name the ids, so an operator (or the agent that just ran ``archive
    reindex`` as a routine fix) can adjudicate against a backup generation
    before the next backup run propagates the new content. Returns ``None``
    with no readable previous index, else ``{"events": n, "sample": [...]}``."""
    if not index_path.exists():
        return None
    conn = sqlite3.connect(tmp_path)
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS old", (str(index_path),))
            # Raw-text inequality is the cheap SQL prefilter; each candidate is
            # confirmed in Python (see _parsed_equal). Streamed, not materialized.
            cur = conn.execute(
                "SELECT o.id, o.event_type, o.payload, n.event_type, n.payload "
                "FROM old.events o JOIN main.events n ON n.id = o.id "
                "WHERE o.payload IS NOT n.payload OR o.event_type IS NOT n.event_type"
            )
            diverged = 0
            sample: list[int] = []
            for ev_id, o_type, o_payload, n_type, n_payload in cur:
                if o_type == n_type and _parsed_equal(o_payload, n_payload):
                    continue
                diverged += 1
                if len(sample) < 10:
                    sample.append(int(ev_id))
        except sqlite3.Error as e:
            # No readable old index — nothing to diverge from; the rebuild IS
            # the recovery.
            logger.warning("reindex: cannot content-diff against the previous index (%s)", e)
            return None
    finally:
        conn.close()
    return {"events": diverged, "sample": sample}


def _build_fk_violations(tmp_path: Path) -> list[tuple]:
    """``PRAGMA foreign_key_check`` over the whole build — the relational gate
    behind fail-closed publication. The loader runs FK-OFF with blanket
    ``INSERT OR REPLACE``, so a load-order accident can delete a parent row
    while its children survive (``threads.name`` is UNIQUE: two thread records
    sharing a name make OR REPLACE silently drop one thread and orphan its
    events). ``quick_check`` is page-level and cannot see that. Every declared
    FK targets ``threads`` — deliberately-soft references (citations to
    events) carry no FK, so this gate can never refuse a rebuild over the
    dangling citations ``verify --deep`` tolerates by design. Returns up to 20
    ``(table, rowid, parent, fkid)`` rows."""
    conn = sqlite3.connect(tmp_path)
    try:
        return conn.execute("PRAGMA foreign_key_check").fetchmany(20)
    finally:
        conn.close()


def _files_for_thread(d: Path, thread_id: int) -> list[Path]:
    """Every truth file for ``thread_id`` — its canonical-depth file plus any
    stale twin at another shard depth."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return []
    return list(threads_dir.rglob(f"{int(thread_id)}.jsonl"))


def _reconcile_collapsed_citations(d: Path, engine) -> dict:
    """Post-load reconciliation of citation → event references in the build.

    Dedup collapse (OR REPLACE + the ``(thread_id, dedup_key)`` unique index)
    keeps one row per content identity; when the *discarded* twin's id was cited
    (``topic_messages.event_id``), the citation would dangle even though the
    content it cites survived under the other id. The truth still holds the
    discarded id's line, so its ``dedup_key`` recovers the surviving row: the
    citation is repointed to it — or dropped when the topic already cites the
    survivor (same content, same topic, one citation). A citation whose event
    has no surviving twin is left dangling for ``verify --deep`` to report —
    but only while its *thread* still exists. When the cited thread is gone
    from the build too (a quarantined contamination, a deliberately removed
    thread), nothing in the store anchors the row: there is no twin to repoint
    to and no parent for its declared ``thread_id`` FK, so the relational gate
    would refuse the rebuild over it. Such a citation is dropped, archived or
    not — the gate stays reserved for genuine loader accidents.

    Citations whose recorded ``thread_id`` disagrees with the cited event's
    actual thread are aligned to the event — the event row is authoritative and
    the column is derived (a wrong value came from an unvalidated write or a
    stale snapshot seed).

    Both repairs are deterministic functions of the truth directory, so
    re-running them on every reindex lands on the same projection; the truth
    log itself is never rewritten here. Returns the (nonzero) counts."""
    with engine.begin() as conn:
        dangling = conn.exec_driver_sql(
            "SELECT m.id, m.topic_id, m.event_id, m.thread_id FROM topic_messages m "
            "WHERE m.archived_at IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = m.event_id)"
        ).fetchall()
    repointed = dropped = 0
    if dangling:
        # dedup_key of each discarded id, recovered from its thread's truth
        # file(s). dedup_key is thread-scoped (same content in two threads
        # shares a key), so the survivor lookup stays scoped to the thread.
        wanted_by_thread: dict[int, set[int]] = {}
        for _, _, ev_id, tid in dangling:
            wanted_by_thread.setdefault(int(tid), set()).add(int(ev_id))
        keys: dict[int, str] = {}
        for tid, wanted in wanted_by_thread.items():
            for path in _files_for_thread(d, tid):
                for rec in _iter_jsonl(path):
                    if (
                        rec.get("type", "event") == "event"
                        and rec.get("id") in wanted and rec.get("dedup_key")
                    ):
                        keys[int(rec["id"])] = rec["dedup_key"]
        with engine.begin() as conn:
            for row_id, topic_id, ev_id, tid in dangling:
                key = keys.get(int(ev_id))
                if not key:
                    continue
                survivor = conn.exec_driver_sql(
                    "SELECT id FROM events WHERE thread_id = ? AND dedup_key = ?",
                    (int(tid), key),
                ).fetchone()
                if survivor is None:
                    continue
                already = conn.exec_driver_sql(
                    "SELECT 1 FROM topic_messages WHERE topic_id = ? AND event_id = ?",
                    (int(topic_id), int(survivor[0])),
                ).fetchone()
                if already is not None:
                    conn.exec_driver_sql(
                        "DELETE FROM topic_messages WHERE id = ?", (int(row_id),)
                    )
                    dropped += 1
                else:
                    conn.exec_driver_sql(
                        "UPDATE topic_messages SET event_id = ? WHERE id = ?",
                        (int(survivor[0]), int(row_id)),
                    )
                    repointed += 1
    with engine.begin() as conn:
        unanchored = conn.exec_driver_sql(
            "DELETE FROM topic_messages "
            "WHERE thread_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.id = topic_messages.event_id) "
            "AND NOT EXISTS(SELECT 1 FROM threads t WHERE t.id = topic_messages.thread_id)"
        ).rowcount
    with engine.begin() as conn:
        aligned = conn.exec_driver_sql(
            "UPDATE topic_messages SET thread_id = "
            "(SELECT e.thread_id FROM events e WHERE e.id = topic_messages.event_id) "
            "WHERE EXISTS(SELECT 1 FROM events e WHERE e.id = topic_messages.event_id "
            "AND e.thread_id != topic_messages.thread_id)"
        ).rowcount
    out: dict = {}
    if repointed:
        out["citations_repointed"] = repointed
    if dropped:
        out["citations_dropped"] = dropped
    if unanchored:
        out["citations_dropped_unanchored"] = unanchored
    if aligned:
        out["citations_thread_aligned"] = aligned
    if out:
        logger.warning("reindex: citation reconciliation %s", out)
    return out


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


def reindex(*, vectors: bool = False, salvage: bool = False) -> dict:
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
    until they reconnect or restart — an accepted stale-read window (long-lived
    readers converge via :func:`thread_archive._api._reconnect_if_swapped`).

    Bulk loading targets the build file through a **FK-OFF Core** loader so
    dependency-agnostic inserts need no ordering and the conversation truth-log
    listeners never fire; the kg-event replay runs through a Session on that same
    engine but stages nothing, so it likewise can't re-write the truth it reads.
    Loads are **INSERT OR REPLACE** (last-wins). The JSONL is authoritative and
    replayed as-is — including any dangling reference; integrity was the writer's
    job.

    **Publication fails closed on committed-content loss.** Unparseable truth
    lines are skipped and counted (``parse_errors``, split into
    ``parse_errors_torn_tail`` — the file's final line, a torn last append — and
    ``parse_errors_interior``): crash artifacts are expected residue (an
    isolated torn fragment stays an unparseable interior line forever) and must
    never block the recovery primitive. What must never happen instead is a
    swap that silently *loses committed records*, so before publishing the
    build is diffed against the current index (:func:`_committed_regression`):
    any event, kg-event, or thread the old index holds that the rebuild lacks —
    by id, and for events with no same-content twin surviving under another id —
    aborts the swap (the old index stays live) and the error names the loss.
    The build must also pass ``PRAGMA foreign_key_check``
    (:func:`_build_fk_violations`) — the FK-OFF OR REPLACE load can orphan
    children when conflicting parent records collide, and a structurally
    inconsistent build must not be published. Deliberately-soft references
    (citations to events) carry no FK, so the gate never blocks the tolerated
    dangling-citation case; ``salvage=True`` overrides it like the loss gate. A
    crash fragment was never committed (truth is fsynced before its COMMIT), so
    it can't trip the gate; a damaged committed line always does. ``salvage=True``
    is the deliberate override: publish the lossy rebuild anyway. With no
    readable previous index (the ``rm index.db`` recovery flow) there is no
    baseline and the gate is skipped — parse errors are still reported. The
    rebuilt file must also pass ``PRAGMA quick_check`` before the swap (never
    overridable) — a build corrupted at the page level must not replace a
    healthy index.

    **Content divergence is reported, never blocked.** A same-id event whose
    content disagrees between the old index and the build is counted into the
    result (``content_overwrites`` + sample ids) and logged at publication
    (:func:`_content_divergence`): the truth is authoritative and its content
    wins, but nothing mutates a payload after commit, so a disagreement means
    one copy is damaged — and once the swap lands the two stores agree and
    cross-store parity can no longer see it. The report is the last observable
    moment of the overwrite; adjudicate an unexpected one against a backup
    generation before the next backup run propagates the published content."""
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
        # Entering the truth-write mutex resolves any crashed drain's leftover
        # intent (a partial batch) before the rebuild reads the files.
        with _truth_write_lock():
            pass
        _unlink_build(tmp_path)  # a dead prior build is stale — start clean
        loader = build_engine(f"sqlite:///{tmp_path}", enforce_fk=False)
        try:
            init_db(loader)
            counts["threads"], counts["events"] = _load_thread_files(d, loader, errors=parse_errors)
            # The loader counts rows loaded; OR REPLACE + the (thread_id, dedup_key)
            # unique index collapse superseded lines (re-appended ids, same-content
            # twins from a lost-commit re-import, a thread's stale twin at another
            # shard depth), so report what actually survived.
            with loader.begin() as conn:
                actual_events = conn.exec_driver_sql("SELECT count(*) FROM events").scalar() or 0
                actual_threads = conn.exec_driver_sql("SELECT count(*) FROM threads").scalar() or 0
            if actual_events != counts["events"]:
                counts["events_collapsed"] = counts["events"] - actual_events
                counts["events"] = int(actual_events)
            if actual_threads != counts["threads"]:
                counts["threads"] = int(actual_threads)
            for name, model in _CROSS_THREAD.items():
                counts[name] = _load_table(model, d / f"{name}.jsonl", loader, errors=parse_errors)
            # Source-import watermarks: seed from the checkpoint snapshot, then
            # overlay the previous live index's fresher rows (see _carry_import_state).
            _load_table(ImportState, d / "import_state.jsonl", loader, errors=parse_errors)
            counts["import_state"] = _carry_import_state(index_path, loader)
            counts["kg_events"] = _replay_kg_events(d, loader, errors=parse_errors)

            # Fail closed on committed-content loss (see docstring): crash
            # fragments never block recovery, but a rebuild missing records the
            # current index holds must not be published over it.
            torn_tails, interior = _classify_parse_errors(parse_errors)
            counts["parse_errors_torn_tail"] = len(torn_tails)
            counts["parse_errors_interior"] = len(interior)
            if not salvage:
                lost = _committed_regression(index_path, tmp_path)
                if lost is not None and (lost["events"] or lost["kg_events"] or lost["threads"]):
                    err_sample = ", ".join(f"{p}:{ln}" for p, ln in interior[:5])
                    raise RuntimeError(
                        f"reindex: the rebuild would lose {lost['events']} committed "
                        f"event(s), {lost['kg_events']} curation event(s) and "
                        f"{lost['threads']} thread(s) the current index holds "
                        f"(event sample: {lost['event_sample']}; thread sample: "
                        f"{lost['thread_sample']}) "
                        "— refusing to publish; the old index was left in place. "
                        + (f"Likely cause: {len(interior)} damaged truth line(s) "
                           f"({err_sample}). " if interior else "")
                        + "Repair the truth (or restore it from backup), or rerun "
                        "with --salvage to publish the lossy rebuild anyway."
                    )
            # Report-only, salvage or not: same-id content the publish will
            # overwrite in the index. The truth wins by design — this is the
            # last observable moment of the overwrite, not a gate (see
            # _content_divergence).
            diverged = _content_divergence(index_path, tmp_path)
            if diverged and diverged["events"]:
                counts["content_overwrites"] = diverged["events"]
                counts["content_overwrite_sample"] = diverged["sample"]
                logger.warning(
                    "reindex: publishing content for %d event id(s) that disagrees "
                    "with the live index (sample: %s) — the truth wins by design; "
                    "if this is unexpected, adjudicate against a backup generation "
                    "before the next backup run propagates it",
                    diverged["events"], diverged["sample"],
                )
            counts.update(_reconcile_collapsed_citations(d, loader))

            # FTS + vectors resolve their engine via get_engine(); point them at
            # the build for the block.
            with use_engine(loader):
                from .._retrieval.fts import rebuild_fts

                counts["fts"] = rebuild_fts()
                # Vectors always survive the rebuild: restore the durable sidecar
                # cache (space-key-guarded; the hours-long embed runs once, ever)
                # into the build regardless of the ``vectors`` flag — a plain
                # reindex must not silently swap away the semantic index.
                # ``vectors=True`` additionally embeds whatever the cache lacks and
                # refreshes the sidecar. Degrades to 0 without a sidecar or the
                # [embeddings] extra — the store stays lexical-only.
                from .._retrieval import vectors as _vec

                counts["vectors_restored"] = _vec.load_vectors_sidecar(d)
                # The sidecar may carry vectors for event ids the rebuild
                # collapsed away (superseded same-content twins) — prune them
                # so the vector arm never scores rows that can't hydrate.
                with loader.begin() as conn:
                    has_vec = conn.exec_driver_sql(
                        "SELECT 1 FROM sqlite_master WHERE name='event_vectors'"
                    ).scalar()
                    pruned = conn.exec_driver_sql(
                        "DELETE FROM event_vectors "
                        "WHERE event_id NOT IN (SELECT id FROM events)"
                    ).rowcount if has_vec else 0
                if pruned:
                    counts["vectors_pruned"] = pruned
                if vectors:
                    counts["vectors_embedded"] = _vec.index_events_local(rebuild=False)
                    counts["vectors_cached"] = _vec.save_vectors_sidecar(d)
        except BaseException:
            loader.dispose()
            _unlink_build(tmp_path)  # the old index was never touched
            raise
        loader.dispose()

        # Relational gate on the FINISHED build (after the reconciliation pass
        # has run its deterministic repairs): the FK-OFF OR REPLACE load can
        # orphan children when conflicting parent records collide, and a
        # structurally inconsistent build must not be published. Salvage
        # overrides, like the loss gate.
        if not salvage:
            fk_violations = _build_fk_violations(tmp_path)
            if fk_violations:
                _unlink_build(tmp_path)
                raise RuntimeError(
                    "reindex: the rebuild is relationally inconsistent — "
                    f"foreign_key_check reported {len(fk_violations)} violation(s) "
                    f"(sample: {fk_violations[:5]}) — refusing to publish; the old "
                    "index was left in place. Likely cause: conflicting thread "
                    "records in the truth (OR REPLACE dropped a parent row). "
                    "Repair the truth, or rerun with --salvage to publish anyway."
                )

        # Page-level gate: a build file corrupted on disk (a bad write during the
        # hours-long rebuild) must not replace a healthy index. Before the WAL
        # fold, and read-write: a WAL-mode database refuses a read-only open
        # once its sidecars are gone.
        qconn = sqlite3.connect(tmp_path)
        try:
            qc = [r[0] for r in qconn.execute("PRAGMA quick_check(10)").fetchall()]
        finally:
            qconn.close()
        if qc != ["ok"]:
            _unlink_build(tmp_path)
            raise RuntimeError(
                f"reindex: rebuilt index failed quick_check ({'; '.join(map(str, qc))}) "
                "— the old index was left in place"
            )
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
    from .._knowledge import reset_cache as _reset_kg

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
    _fsync_dir(path.parent)  # the rename itself must survive power loss
    return n_ev


def _hash_key_check(payload: object, dedup_key: str) -> bool | None:
    """True = the payload re-hashes to the content hash embedded in its own
    ``dedup_key`` (the last ``:``-segment; see
    ``thread_archive._thread_import.event_builder.compute_dedup_key``); False = mismatch;
    None = the key carries no hash tail (nothing to validate against)."""
    import re as _re

    from thread_archive._thread_import.event_builder import compute_content_hash

    if not _re.match(r"^[0-9a-f]{16}$", dedup_key.rsplit(":", 1)[-1]):
        return None
    if not isinstance(payload, dict):
        return False
    return compute_content_hash(payload) == dedup_key.rsplit(":", 1)[-1]


def _store_rows_failing_key_hash() -> tuple[int, list[str]]:
    """Store event rows whose payload no longer re-hashes to the content hash
    embedded in their own ``dedup_key`` — the content half of the pre-flight
    behind :func:`rebuild_truth_from_store`. The containment check proves the
    store holds every truth *unit*; this proves the payloads behind those units
    are self-consistent, so a corrupted index row (rot, a bad in-place write)
    that kept its id and key can't be promoted over the good truth line by a
    re-emit. Returns ``(failing, sample)``."""
    failing = 0
    sample: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        for ev_id, tid, key, payload_text in conn.execute(
            "SELECT id, thread_id, dedup_key, payload FROM events "
            "WHERE dedup_key IS NOT NULL"
        ):
            try:
                payload = (
                    json.loads(payload_text)
                    if isinstance(payload_text, str) else payload_text
                )
            except ValueError:
                payload = None
            if _hash_key_check(payload, key) is False:
                failing += 1
                if len(sample) < 10:
                    sample.append(f"thread {tid}: event {ev_id}")
    return failing, sample


def _truth_units_missing_from_store(d: Path) -> tuple[int, list[str], dict[str, int]]:
    """Content units present in the truth but absent from the store — the
    pre-flight behind :func:`rebuild_truth_from_store`. A *unit* is an event's
    content identity: its ``dedup_key`` (thread-scoped), falling back to the
    event id when the key is NULL — the same collapse ``scan_truth_counts`` and
    the reindex loader apply. Count parity can hide compensating errors (an
    index-only event masking a missing one, swapped payloads behind equal
    totals); containment can't: every effective truth unit must exist in the
    store, or a re-emit would destroy content the index never had. Walked one
    thread at a time so memory stays bounded.

    The same walk collects ``dropped_keys``: fields on truth records (event,
    thread, kg_event) that the running code's models don't map. ``_coerce``
    drops them on reload — harmless for the projection — but a re-emit rewrites
    the truth *without* them, which turns that tolerance into permanent loss;
    the caller refuses on any. Returns ``(missing, sample, dropped_keys)``
    where ``dropped_keys`` maps ``"<kind>.<field>"`` to its occurrence count."""
    threads_dir = d / THREADS_SUBDIR
    files_by_stem: dict[str, list[Path]] = {}
    if threads_dir.exists():
        for path in threads_dir.rglob("*.jsonl"):
            files_by_stem.setdefault(path.stem, []).append(path)
    valid_keys = {
        "event": {c.key for c in Event.__table__.columns},
        "thread": {c.key for c in Thread.__table__.columns},
        "kg_event": {c.key for c in KgEvent.__table__.columns},
    }
    dropped: dict[str, int] = {}

    def _note_unknown(kind: str, rec: dict) -> None:
        for k in rec.keys() - valid_keys[kind] - {"type"}:
            dropped[f"{kind}.{k}"] = dropped.get(f"{kind}.{k}", 0) + 1

    missing = 0
    sample: list[str] = []
    with get_session() as s:
        conn = s.connection().connection  # raw sqlite3 — stream, don't materialize
        for stem, paths in files_by_stem.items():
            try:
                tid = int(stem)
            except ValueError:  # pragma: no cover — stray file
                continue
            units: set = set()
            for path in paths:
                for rec in _iter_jsonl(path):
                    kind = rec.get("type", "event")
                    if kind in valid_keys:
                        _note_unknown(kind, rec)
                    if kind != "event" or rec.get("id") is None:
                        continue
                    units.add(rec.get("dedup_key") or ("id", int(rec["id"])))
            if not units:
                continue
            for ev_id, key in conn.execute(
                "SELECT id, dedup_key FROM events WHERE thread_id = ?", (tid,)
            ):
                units.discard(key or ("id", int(ev_id)))
            missing += len(units)
            for unit in sorted(map(str, units))[: max(0, 10 - len(sample))]:
                sample.append(f"thread {tid}: {unit}")
    if (d / KG_EVENTS_FILE).exists():
        for rec in _iter_jsonl(d / KG_EVENTS_FILE):
            _note_unknown("kg_event", rec)
    return missing, sample, dropped


def rebuild_truth_from_store(*, force: bool = False) -> dict:
    """Re-emit the entire per-thread truth from the current SQLite store.

    The inverse of :func:`reindex`: for every thread, (re)write ``threads/<id>.jsonl``
    as its metadata record + ordered events; rewrite the cross-thread snapshots; pick
    the shard depth for the thread count; set the manifest. Used to migrate an
    older monolithic ``events.jsonl`` into per-thread files (the old monolith files,
    if present, are removed), and to finish an index-only repair pass (e.g. the
    dedup-key backfill) by making the truth match. Idempotent — safe to re-run.

    This is the ONE operation that overwrites truth from the projection — the
    reverse of the normal flow — so it protects itself: it holds the reindex lock
    **exclusive** for the duration (no writer can append truth or commit to the
    index mid-emission; in-tree writers all hold it shared), and it refuses to run
    unless the store *contains* every effective content unit the truth holds
    (:func:`_truth_units_missing_from_store` — per-unit containment, not count
    parity, so a missing event can't hide behind an index-only one). A repair
    pass that rewrites payloads in place keeps its units (the ``dedup_key``
    column carries the identity), so the intended use survives the gate.

    Two further pre-flights guard the *content* of what gets written: the truth
    must carry no fields the running code's models don't map (``_coerce``
    tolerates them on reload, but a re-emit would drop them from the truth
    forever — an older binary must not lossily rewrite newer truth), and every
    store payload with a hash-tailed ``dedup_key`` must still re-hash to it
    (:func:`_store_rows_failing_key_hash` — a corrupted index row that kept its
    id and key must not replace the good truth line).

    ``force=True`` overrides the pre-flights only (for a deliberate, understood
    shrink or drop — e.g. a duplicate-collapse repair, or an in-place payload
    repair that didn't recompute its keys); it never skips the lock."""
    d = log_dir()
    (d / THREADS_SUBDIR).mkdir(parents=True, exist_ok=True)

    with _hold_reindex_lock():
        # Resolve any crashed drain's leftover intent first: a stale intent
        # surviving past the re-emit would frame baselines that no longer
        # describe the (replaced) files. The tail check would refuse to cut
        # them anyway; resolving here keeps that guard a backstop, not a path.
        with _truth_write_lock():
            pass
        if not force:
            missing, sample, dropped = _truth_units_missing_from_store(d)
            if missing:
                raise RuntimeError(
                    f"rebuild_truth_from_store: the store lacks {missing} event(s) "
                    f"the truth holds (sample: {sample}) — re-emitting would destroy "
                    "truth content the index lacks. Run `archive reindex` first "
                    "(or pass force=True if the shrink is intended)."
                )
            if dropped:
                fields = ", ".join(f"{k} ×{v}" for k, v in sorted(dropped.items()))
                raise RuntimeError(
                    f"rebuild_truth_from_store: the truth carries field(s) the running "
                    f"code's models don't map ({fields}) — re-emitting would silently "
                    "drop them from the truth forever. Run the code version that wrote "
                    "them (or pass force=True if the drop is intended)."
                )
            failing, bad_sample = _store_rows_failing_key_hash()
            if failing:
                raise RuntimeError(
                    f"rebuild_truth_from_store: {failing} store payload(s) fail their "
                    f"own dedup-key content hash (sample: {bad_sample}) — re-emitting "
                    "would promote suspect index content over the existing truth. "
                    "Investigate with `archive verify --hashes` and repair the index "
                    "(`archive reindex`) first, or pass force=True if the payloads are "
                    "known-good (an in-place repair that didn't recompute its keys)."
                )
        return _rebuild_truth_from_store_locked(d)


def _rebuild_truth_from_store_locked(d: Path) -> dict:
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

    # Locked read-modify-write: the re-emit owns the layout keys, not the whole
    # manifest — a verify run's hashes baseline (or any future key) survives.
    update_manifest(d, lambda m: m.update(
        {"version": TRUTH_FORMAT_VERSION, "shard_depth": depth, "last_checkpoint_at": _now_iso()}
    ))

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
