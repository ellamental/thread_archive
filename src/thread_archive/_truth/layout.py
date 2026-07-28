"""Truth-directory layout: paths, manifest, sharding, record serialization.

The on-disk vocabulary of the JSONL truth store — where a thread's file lives
(flat or id-bucket sharded, per the manifest's ``shard_depth``), how the
manifest is read/written (atomic replace under the manifest flock), and how ORM
rows round-trip to JSONL records (``_row_dict`` / ``_coerce``). Storage-model
overview: :mod:`.jsonl_log`.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy import DateTime

from .._store import ThreadLink, TopicMessage

logger = logging.getLogger(__name__)

# Per-thread files live under truth/threads/; the cross-thread overlays are
# snapshot files (full-rewritten at checkpoint) since they aren't one thread's
# content. thread_links / topic_messages FK threads.id, so they load after threads.
THREADS_SUBDIR = "threads"
_CROSS_THREAD: dict[str, type] = {"thread_links": ThreadLink, "topic_messages": TopicMessage}

# The topic-graph event log (see KgEvent): an append-only file of every topic/link/
# evidence mutation. It is the *source of truth* for the knowledge layer — the
# thread_links / topic_messages projections are folded from it on reindex. A
# _CROSS_THREAD snapshot, if present, is treated as a reindex seed the replay
# reconciles on top (upsert + tombstone).
KG_EVENTS_FILE = "kg_events.jsonl"

_BUCKET = 256  # children per shard level


def flat_max() -> int:
    """Files a directory may hold before the layout shards by id buckets.

    Read per call from ``THREAD_ARCHIVE_SHARDFLAT_MAX``: the threshold is
    configuration, and a constant would answer it once at import and ignore an
    override set after this module loads.
    """
    return int(os.environ.get("THREAD_ARCHIVE_SHARDFLAT_MAX", "16384"))

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
# Version 2: thread ids are ULID strings (integer alias in ``legacy_id``);
# shard buckets hash the id string instead of taking integer modulos.
TRUTH_FORMAT_VERSION = 2

# The v1→v2 migration's durable legacy-id → ULID record, written beside the
# truth dir (at the home root) by ``_scripts.migrate_thread_ulids``. Consumers
# (e.g. the backup mirror's renamed-twin detection) treat a missing or
# unreadable file as "never migrated".
ULID_MAPPING_FILE = "ulid-mapping.json"


class TruthFormatError(RuntimeError):
    """The truth directory cannot be safely interpreted or mutated by this code."""


class TruthMigrationRequired(TruthFormatError):
    """A writer met an older truth format that must be migrated first."""


def _manifest_path(d: Path) -> Path:
    return d / "manifest.json"


# Parsed-manifest cache, keyed by the file's exact identity (path, mtime_ns,
# inode, size). The drain reads the shard depth on every commit batch, so the
# manifest would otherwise be opened and JSON-parsed a few times a second under
# live ingest. _write_manifest publishes by os.replace (new inode, new mtime),
# so any write — this process's or another's — changes the key and forces a
# re-read; the no-manifest / corrupt-manifest paths are never cached (their
# result depends on directory layout, not on a file this key can witness).
# Unsynchronized on purpose: a racing refresh just parses twice.
_manifest_cache: tuple[tuple[str, int, int, int], dict] | None = None


def _manifest_stat_key(p: Path) -> tuple[str, int, int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p), st.st_mtime_ns, st.st_ino, st.st_size)


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
            with os.scandir(cur) as entries:
                bucket = next(
                    (
                        e for e in entries
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


def _infer_format_version(d: Path) -> int:
    """Infer v1 when any per-thread filename still carries an integer id.

    Missing/corrupt manifests are already a recovery path.  Treating an old
    integer tree as current here would let the v2 writer add ULID records while
    continuing to advertise v1 — exactly the cross-version corruption the
    manifest is meant to prevent.  A mixed tree is therefore v1 until the
    migration has normalized the whole directory.
    """
    threads = d / THREADS_SUBDIR
    try:
        return 1 if any(p.stem.isdigit() for p in threads.rglob("*.jsonl")) else TRUTH_FORMAT_VERSION
    except OSError:
        return TRUTH_FORMAT_VERSION


def _read_manifest(d: Path) -> dict:
    global _manifest_cache
    p = _manifest_path(d)
    key = _manifest_stat_key(p)
    if key is not None and _manifest_cache is not None and _manifest_cache[0] == key:
        return copy.deepcopy(_manifest_cache[1])
    if p.exists():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt manifest: fall through to defaults, but NEVER default the
            # shard depth on a sharded archive — infer it from the layout below.
            logger.error("truth: manifest.json unreadable — inferring shard depth")
        else:
            declared = m.get("version")
            if not isinstance(declared, int) or declared < 1:
                declared = _infer_format_version(d)
                m["version"] = declared
            if isinstance(declared, int) and declared > TRUTH_FORMAT_VERSION:
                # A newer layout may have changed sharding or record semantics;
                # reading it with old assumptions risks writing flat twins or
                # shadowing fresh records — refuse instead of guessing.
                raise TruthFormatError(
                    f"truth directory {d} declares format version {declared}, but this "
                    f"code reads version {TRUTH_FORMAT_VERSION}. Upgrade thread-archive "
                    "to a release that supports it."
                )
            if key is not None:
                _manifest_cache = (key, copy.deepcopy(m))
            return m
    depth = _infer_shard_depth(d)
    if depth:
        logger.warning(
            "truth: no readable manifest; inferred shard_depth=%d from layout", depth
        )
    return {
        "version": _infer_format_version(d),
        "shard_depth": depth,
        "last_checkpoint_at": None,
    }


def require_current_format(d: Path | None = None) -> None:
    """Fail before a current-format writer mutates older truth.

    Older truth remains readable so migration and reindex can recover it.  It
    must not be appended to, snapshotted, repaired, or rewritten with newer
    record/layout semantics while its manifest still promises an old reader
    that the directory is old-format.
    """
    d = log_dir() if d is None else d
    declared = int(_read_manifest(d).get("version", 1))
    if declared < TRUTH_FORMAT_VERSION:
        raise TruthMigrationRequired(
            f"truth directory {d} uses format version {declared}, but this writer "
            f"emits version {TRUTH_FORMAT_VERSION}. Reads remain available; run "
            "`thread-archive index migrate` before writing."
        )


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
def _thread_relpath(thread_id: str, depth: int) -> Path:
    """Relative path of a thread's file at ``depth``: flat (depth 0), else nested
    two-hex-digit bucket dirs taken from the sha256 of the id string (byte ``i``
    names the level-``i`` bucket). Deterministic — readers and writers compute
    the same location from the manifest's depth; hashing keeps buckets uniform
    regardless of the id format's structure (ULIDs share a timestamp prefix)."""
    tid = str(thread_id)
    parts: list[str] = []
    if depth:
        digest = hashlib.sha256(tid.encode("utf-8")).hexdigest()
        parts = [digest[2 * i : 2 * i + 2] for i in range(depth)]
    return Path(THREADS_SUBDIR, *parts, f"{tid}.jsonl")


def _thread_file(d: Path, thread_id: str, depth: int) -> Path:
    return d / _thread_relpath(thread_id, depth)


def _max_dir_occupancy(n_threads: int, depth: int) -> int:
    """Worst-case files in a single directory for ``n_threads`` spread over ``depth``
    bucket levels (256 buckets/level, roughly uniform by id)."""
    return -(-n_threads // (_BUCKET ** depth))  # ceil div


def _depth_for(n_threads: int) -> int:
    depth = 0
    cap = flat_max()
    while _max_dir_occupancy(n_threads, depth) > cap:
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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _coerce(model: type, row: dict) -> dict:
    """Reverse :func:`_json_default` for reload: ISO strings → datetime so the
    SQLite DateTime columns round-trip as the live writer wrote them. Keys not
    mapped on the model are dropped, so a truth log written under an older schema
    (columns since removed) still reloads cleanly into the current projection.

    A rotted datetime value (bit-flip at rest, a truncated write that still parsed
    as JSON) must not abort the whole reindex — the recovery primitive has to keep
    every parseable record, matching :func:`_iter_jsonl`'s tolerance of torn lines.
    An unparseable value is logged and replaced: NULL where the column allows it,
    the Unix epoch where it doesn't (a recognizable sentinel that satisfies NOT
    NULL and sorts the salvaged record to the far past rather than dropping it)."""
    columns = cast(Any, model).__table__.columns
    valid = {c.key for c in columns}
    row = {k: v for k, v in row.items() if k in valid}
    for key in _datetime_cols(model):
        val = row.get(key)
        if isinstance(val, str):
            try:
                row[key] = datetime.fromisoformat(val)
            except ValueError:
                fallback = None if columns[key].nullable else _EPOCH
                logger.warning(
                    "truth: unparseable datetime %s.%s=%r — substituting %s",
                    model.__name__, key, val, "null" if fallback is None else "epoch",
                )
                row[key] = fallback
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


def _iter_jsonl(
    path: Path, *, errors: list[tuple[str, int]] | None = None, log: bool = True
):
    """Yield parsed records from a JSONL file, skipping unparseable lines.

    A torn line (a crash mid-append) must not kill :func:`reindex` — the recovery
    primitive has to recover everything parseable, with the same tolerance
    ``thread-archive index verify`` (:func:`scan_truth_counts`) already has. Every skipped line
    is logged, and recorded on ``errors`` as ``(path, lineno)`` when given, so
    reindex can report the count instead of silently dropping. Pass ``log=False``
    for a second pass over lines ``scan_truth_counts`` already logged/classified on
    this run (verify's deep scan, repair's containment read), so the same torn line
    isn't warned about twice."""
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
                if log:
                    logger.warning("truth: skipping unparseable line %s:%d", path, lineno)
                if errors is not None:
                    errors.append((str(path), lineno))
