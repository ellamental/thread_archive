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
always agree; crossing the threshold triggers a one-time auto-rebalance (the only
time files move). Small archives stay flat with zero ceremony.

Writes preserve the **JSONL ⊇ SQLite** invariant: a row is staged on the session
and flushed to its thread file *before* the COMMIT it belongs to, so the projection
can never hold a row the truth lacks. :func:`reindex` is the recovery primitive —
it rebuilds the SQLite store from the JSONL directory; a corrupted or deleted index
is never a data-loss event. :func:`rebuild_truth_from_store` is the inverse: it
re-emits the whole per-thread truth from the current store (used once to migrate an
older monolithic ``events.jsonl`` into per-thread files).
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from sqlalchemy import DateTime, delete, event, insert, select
from sqlalchemy.orm import Session

from ..store import (
    Event,
    KgEvent,
    Thread,
    ThreadLink,
    TopicMessage,
    build_engine,
    get_engine,
    get_session,
    init_db,
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
    tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
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
def _handle(path: Path) -> TextIO:
    key = str(path)
    fh = _handles.get(key)
    if fh is not None and not fh.closed:
        _handles.move_to_end(key)
        return fh
    path.parent.mkdir(parents=True, exist_ok=True)
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
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    d = log_dir()
    depth = _shard_depth(d)
    for kind, thread_id, row in pending:
        if kind == "kg_event":
            _append_line(d / KG_EVENTS_FILE, {"type": kind, **row})
        else:
            _append_line(_thread_file(d, thread_id, depth), {"type": kind, **row})


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
    with get_session() as s:
        changed = s.execute(select(Thread).where(Thread.updated_at > last_dt)).scalars().all()
        for t in changed:
            _append_line(_thread_file(d, t.id, depth), {"type": "thread", **_row_dict(t)})
            n += 1
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
    depth = int(m.get("shard_depth", 0))
    counts: dict = (
        {name: _write_snapshot(d, name, model) for name, model in _CROSS_THREAD.items()}
        if snapshots
        else {}
    )
    counts["threads_updated"] = _checkpoint_changed_threads(d, depth, m.get("last_checkpoint_at"))
    m["shard_depth"] = _maybe_rebalance(d, depth)
    m["last_checkpoint_at"] = _now_iso()
    _write_manifest(d, m)
    logger.info("jsonl_log checkpoint(snapshots=%s): %s", snapshots, counts)
    return counts


# ── adaptive rebalance (flat → sharded when a directory gets large) ──────────
def _maybe_rebalance(d: Path, depth: int) -> int:
    """Bump ``shard_depth`` and move files if the current depth would overflow a
    directory. A no-op for small archives; only fires when crossing a threshold."""
    threads_dir = d / THREADS_SUBDIR
    if not threads_dir.exists():
        return depth
    n = sum(1 for _ in threads_dir.rglob("*.jsonl"))
    target = _depth_for(n)
    if target <= depth:
        return depth
    reset_handles()  # don't move files out from under open handles
    for path in list(threads_dir.rglob("*.jsonl")):
        try:
            tid = int(path.stem)
        except ValueError:  # pragma: no cover — stray file
            continue
        dest = _thread_file(d, tid, target)
        if dest == path:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)
    logger.info("jsonl_log rebalance: shard_depth %d → %d (%d threads)", depth, target, n)
    return target


# ── reindex (rebuild the SQLite projection from the JSONL truth) ─────────────
def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _load_table(model: type, path: Path, engine, batch: int = 5000) -> int:
    """Bulk-load a snapshot file (one row per line) into its table."""
    table = model.__table__  # type: ignore[attr-defined]
    total = 0
    buf: list[dict] = []

    def _flush() -> None:
        nonlocal total
        if not buf:
            return
        with engine.begin() as conn:
            conn.execute(insert(table), buf)
        total += len(buf)
        buf.clear()

    for row in _iter_jsonl(path):
        buf.append(_coerce(model, row))
        if len(buf) >= batch:
            _flush()
    _flush()
    return total


def _load_thread_files(d: Path, engine, batch: int = 5000) -> tuple[int, int]:
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
                conn.execute(insert(Thread.__table__), thread_buf)
            nt += len(thread_buf)
            thread_buf.clear()
        if event_buf:
            with engine.begin() as conn:
                conn.execute(insert(Event.__table__), event_buf)
            ne += len(event_buf)
            event_buf.clear()

    if threads_dir.exists():
        for path in sorted(threads_dir.rglob("*.jsonl")):
            last_thread: dict | None = None
            events: list[dict] = []
            for rec in _iter_jsonl(path):
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


def _replay_kg_events(d: Path, engine) -> int:
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

    rows = list(_iter_jsonl(d / KG_EVENTS_FILE))
    for r in rows:
        r.pop("type", None)
    rows.sort(key=lambda r: r.get("id") or 0)
    if not rows:
        return 0
    coerced = [_coerce(KgEvent, r) for r in rows]
    with engine.begin() as conn:
        conn.execute(insert(KgEvent.__table__), coerced)
    with Session(engine) as s:
        for r in coerced:
            apply_event(s, KgEvent(**r))
        s.commit()
    return len(rows)


def reindex(*, vectors: bool = False) -> dict:
    """Rebuild the SQLite store from the JSONL truth directory.

    The recovery primitive: ensure the schema, clear the projected tables, load every
    per-thread file (threads + events) and the cross-thread snapshots, replay the
    curatorial event log onto the knowledge projection, then rebuild the FTS surface
    (and, optionally, restore/refresh the vector cache). Bulk loading targets the active
    global engine through a **FK-OFF Core** loader so dependency-agnostic inserts need no
    ordering and the conversation truth-log listeners never fire; the kg-event replay
    runs through a Session on that same engine but stages nothing, so it likewise can't
    re-write the truth it reads. The JSONL is authoritative and replayed as-is —
    including any dangling reference; integrity was the writer's job."""
    d = log_dir()

    engine = get_engine()
    init_db(engine)

    loader = build_engine(str(engine.url), enforce_fk=False)
    counts: dict = {}
    try:
        with loader.begin() as conn:
            for model in (KgEvent, TopicMessage, ThreadLink, Event, Thread):  # children before parents
                conn.execute(delete(model))
        counts["threads"], counts["events"] = _load_thread_files(d, loader)
        for name, model in _CROSS_THREAD.items():
            counts[name] = _load_table(model, d / f"{name}.jsonl", loader)
        counts["kg_events"] = _replay_kg_events(d, loader)
    finally:
        loader.dispose()

    # Rebuild the FTS surface (events_fts shadow + the FTS5 event_search table).
    from ..retrieval.fts import rebuild_fts

    counts["fts"] = rebuild_fts()

    # The topic graph caches a projection per engine; drop it so the next read
    # rebuilds over the freshly-loaded thread_links.
    from ..knowledge import reset_cache as _reset_kg

    _reset_kg()

    if vectors:
        # Restore the durable vector cache (the hours-long embed runs once, ever),
        # embed only genuinely-new events, then refresh the sidecar. Degrades to 0
        # without the [embeddings] extra — the store stays lexical-only.
        from ..retrieval import vectors as _vec

        counts["vectors_restored"] = _vec.load_vectors_sidecar(d)
        counts["vectors_embedded"] = _vec.index_events_local(rebuild=False)
        counts["vectors_cached"] = _vec.save_vectors_sidecar(d)

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
        for t in threads:  # outer list is materialized, so the inner event stream is the only cursor
            ev_rows = (_row_dict(ev) for ev in s.execute(
                select(Event).where(Event.thread_id == t.id).order_by(Event.id)
            ).scalars())
            ne += emit_thread_file(d, t.id, depth, _row_dict(t), ev_rows)
            nt += 1

    for name, model in _CROSS_THREAD.items():
        _write_snapshot(d, name, model)

    _write_manifest(d, {"version": 1, "shard_depth": depth, "last_checkpoint_at": _now_iso()})

    # Drop the old monolithic files this format replaces.
    for old in ("events.jsonl", "threads.jsonl"):
        p = d / old
        if p.exists():
            p.unlink()

    result = {"threads": nt, "events": ne, "shard_depth": depth}
    logger.info("jsonl_log rebuild_truth_from_store: %s", result)
    return result
