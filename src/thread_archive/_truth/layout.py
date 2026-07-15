"""Truth-directory layout: paths, manifest, sharding, record serialization.

The on-disk vocabulary of the JSONL truth store — where a thread's file lives
(flat or id-bucket sharded, per the manifest's ``shard_depth``), how the
manifest is read/written (atomic replace under the manifest flock), and how ORM
rows round-trip to JSONL records (``_row_dict`` / ``_coerce``). Storage-model
overview: :mod:`.jsonl_log`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime

from .._store import ThreadLink, TopicMessage

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

