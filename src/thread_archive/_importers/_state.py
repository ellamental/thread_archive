"""Thread resolution + import-state watermarks — the SQLite slice that incremental
import needs.

A thread's identity is its ``(source, source_id)`` and its ``name`` is the
deterministic ``"{source}:{source_id}"`` (also the unique key), since we always
resolve by source before creating.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .._store import Event, ImportState, Thread

# Placeholder model values that aren't a real model — skipped when denormalizing a
# thread's models list (mirrors canonical NON_MODEL_VALUES).
NON_MODEL_VALUES = ("", "unknown", "<synthetic>")


def get_thread_by_source(session: Session, source: str, source_id: str) -> Optional[Thread]:
    return session.execute(
        select(Thread).where(Thread.source == source, Thread.source_id == source_id)
    ).scalars().first()


def lookup_parent_thread(
    session: Session, parent_source_id: str, *, source: str = "claude-code"
) -> Optional[str]:
    """Resolve a parent session's thread_id by ``(source, parent_source_id)`` —
    the import-state watermark first (authoritative even before the thread row is
    visible), then the threads table. None when unknown."""
    state = get_import_state(session, source, parent_source_id)
    if state and state.thread_id:
        return state.thread_id
    thread = get_thread_by_source(session, source, parent_source_id)
    return thread.id if thread else None


def adopt_if_unwatermarked(
    session: Session,
    *,
    source: str,
    source_id: str,
    thread_id: Optional[str],
    total_lines: int,
    file_size: int,
    content_hash: Optional[str] = None,
) -> bool:
    """Guard against re-importing a thread whose ingest cursor was lost.

    The last-resort path for a thread that exists with no ``import_state`` at all —
    e.g. a bulk-seeded archive whose events were loaded outside the incremental
    importers. (Reindex carries the watermarks over — from the previous index and the
    ``import_state.jsonl`` checkpoint snapshot — so a rebuild alone no longer lands
    here.) Re-importing such a file from line 0 would re-insert events the truth
    already holds: their stored ``dedup_key`` need not match a fresh import's, so the
    dedup check wouldn't catch them and every event would double. When the thread
    already has events, we instead stamp the watermark at the file's current EOF and
    skip — the existing events stand, and only genuinely-new appended lines import on
    later polls. The cost is that source lines never imported before adoption are
    skipped for good, which is why the watermarks are kept durable and this path is
    kept rare. Returns True when it adopted (caller must not import)."""
    if thread_id is None:
        return False
    has_events = session.execute(
        select(Event.id).where(Event.thread_id == thread_id).limit(1)
    ).first() is not None
    if not has_events:
        return False
    upsert_import_state(
        session,
        source=source,
        source_id=source_id,
        thread_id=thread_id,
        last_line_count=total_lines,
        last_file_size=file_size,
        last_content_hash=content_hash,
        last_message_uuid=None,
    )
    return True


def create_thread(
    session: Session,
    *,
    source: str,
    source_id: str,
    title: Optional[str] = None,
    thread_type: str = "conversation",
    description: Optional[str] = None,
    source_metadata: Optional[dict] = None,
    exclude_from_search: bool = False,
    started_at: Optional[datetime] = None,
) -> str:
    """Create a thread for ``(source, source_id)`` and return its id (flushed).

    The id is a ULID — the single choke point where new thread ids come into
    being. ``started_at`` (when the importer knows it, e.g. a historical export)
    stamps the ULID's timestamp with the conversation's real start so id order
    matches history; omitted, the id carries creation time, which for live
    tailing is the same thing."""
    from .._store import mint_ulid

    tid = mint_ulid(
        int(started_at.timestamp() * 1000) if started_at is not None else None
    )
    thread = Thread(
        id=tid,
        name=f"{source}:{source_id}",
        title=title,
        thread_type=thread_type,
        description=description,
        source=source,
        source_id=source_id,
        source_metadata=source_metadata,
        exclude_from_search=exclude_from_search,
    )
    session.add(thread)
    session.flush()
    # Stage the thread's metadata record so its truth file opens with it (written
    # to threads/<id>.jsonl on commit, before the events that follow).
    from .._truth.jsonl_log import record_thread

    record_thread(session, thread)
    return thread.id


def discard_new_thread(session: Session, thread_id: str) -> None:
    """Remove a just-created thread that imported nothing — row AND staged truth.

    The complement of :func:`create_thread` for the empty-import cleanup path.
    Deleting only the row is not enough: ``create_thread`` staged the thread's
    metadata record for its truth file, and a commit would still write that
    ``threads/<id>.jsonl`` ghost — which ``verify`` counts as drift and the next
    reindex resurrects as an empty thread. Only for threads created in THIS
    session's transaction (nothing else may reference them yet)."""
    session.execute(delete(Thread).where(Thread.id == thread_id))
    from .._truth.jsonl_log import unstage_thread

    unstage_thread(session, thread_id)


def _restage_thread(session: Session, thread: Thread) -> None:
    """Bump ``updated_at`` and re-stage the thread's metadata record to truth.

    A metadata change after creation (title resync, description/models backfill)
    must reach the JSONL truth or a ``reindex`` would lose it. ``record_thread``
    appends a fresh ``{"type": "thread", ...}`` record (latest wins on reindex),
    and the bumped ``updated_at`` also lets the checkpoint backstop pick it up."""
    thread.updated_at = datetime.now(timezone.utc)
    session.flush()
    from .._truth.jsonl_log import record_thread

    record_thread(session, thread)


def update_thread_title(session: Session, thread_id: int, title: str) -> bool:
    """Set a thread's title and re-stage it to truth. No-op if unchanged/missing."""
    thread = session.get(Thread, thread_id)
    if not thread or thread.title == title:
        return False
    thread.title = title
    _restage_thread(session, thread)
    return True


def update_thread_description(session: Session, thread_id: int, description: str) -> bool:
    """Set a thread's one-line description and re-stage it to truth."""
    thread = session.get(Thread, thread_id)
    if not thread:
        return False
    thread.description = description
    _restage_thread(session, thread)
    return True


def set_thread_models_from_events(session: Session, thread_id: int) -> list[str]:
    """Denormalize a thread's models into ``source_metadata['models']`` from its
    ``api_request_completed`` events (distinct, first-appearance order), so new
    imports land in the sidebar's model filter. Falls back to the operative
    ``source_metadata['model']`` when no model appears in the events. Pure
    re-derivation (overwrites any prior value); re-stages truth only on change.

    SQLite analogue of canonical ``set_thread_models_from_events`` — ``json_extract``
    replaces the Postgres ``payload['model'].astext``."""
    thread = session.get(Thread, thread_id)
    if not thread:
        return []
    model_col = func.json_extract(Event.payload, "$.model")
    rows = session.execute(
        select(model_col)
        .where(Event.thread_id == thread_id)
        .where(Event.event_type == "api_request_completed")
        .where(model_col.is_not(None))
        .where(model_col.notin_(NON_MODEL_VALUES))
        .order_by(Event.id.asc())
    ).scalars().all()
    models: list[str] = []
    for m in rows:
        if m and m not in models:
            models.append(m)
    if not models:
        operative = (thread.source_metadata or {}).get("model")
        if operative:
            models = [operative]
    if not models:
        return []
    if (thread.source_metadata or {}).get("models") == models:
        return models
    # Reassign a fresh dict so SQLAlchemy flags the JSON column dirty.
    meta = dict(thread.source_metadata or {})
    meta["models"] = models
    thread.source_metadata = meta
    _restage_thread(session, thread)
    return models


def last_import_epoch_ms(state: ImportState) -> float:
    """Epoch millis of ``state.last_import_at``. The stamp is written aware-UTC
    but SQLite round-trips it naive, so a naive value IS UTC — calling
    ``.timestamp()`` on it directly would read it as local time and skew every
    watermark comparison by the machine's UTC offset."""
    dt = state.last_import_at
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


def get_import_state(session: Session, source: str, source_id: str) -> Optional[ImportState]:
    return session.execute(
        select(ImportState).where(ImportState.source == source, ImportState.source_id == source_id)
    ).scalars().first()


def upsert_import_state(
    session: Session,
    *,
    source: str,
    source_id: str,
    thread_id: Optional[int],
    last_line_count: int,
    last_file_size: int,
    last_message_uuid: Optional[str],
    last_content_hash: Optional[str] = None,
) -> ImportState:
    """Insert or update the ``(source, source_id)`` watermark (no commit).

    ``last_content_hash`` is the digest of the source bytes this watermark was
    computed over — the file sources pass it so the next poll can prove the cursor
    still points into the same content; the DB-backed sources leave it None.
    """
    state = get_import_state(session, source, source_id)
    if state is None:
        state = ImportState(source=source, source_id=source_id)
        session.add(state)
    state.thread_id = thread_id
    state.last_line_count = last_line_count
    state.last_file_size = last_file_size
    state.last_content_hash = last_content_hash
    state.last_message_uuid = last_message_uuid
    state.last_import_at = datetime.now(timezone.utc)
    return state
