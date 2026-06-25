"""Thread resolution + import-state watermarks — the SQLite slice that incremental
import needs.

A thread's identity is its ``(source, source_id)`` and its ``name`` is the
deterministic ``"{source}:{source_id}"`` (also the unique key), since we always
resolve by source before creating.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..store import ImportState, Thread


def get_thread_by_source(session: Session, source: str, source_id: str) -> Optional[Thread]:
    return session.execute(
        select(Thread).where(Thread.source == source, Thread.source_id == source_id)
    ).scalars().first()


def create_thread(
    session: Session,
    *,
    source: str,
    source_id: str,
    title: Optional[str] = None,
    thread_type: str = "conversation",
    source_metadata: Optional[dict] = None,
) -> int:
    """Create a thread for ``(source, source_id)`` and return its id (flushed)."""
    thread = Thread(
        name=f"{source}:{source_id}",
        title=title,
        thread_type=thread_type,
        source=source,
        source_id=source_id,
        source_metadata=source_metadata,
    )
    session.add(thread)
    session.flush()
    # Stage the thread's metadata record so its truth file opens with it (written
    # to threads/<id>.jsonl on commit, before the events that follow).
    from ..truth.jsonl_log import record_thread

    record_thread(session, thread)
    return thread.id


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
) -> ImportState:
    """Insert or update the ``(source, source_id)`` watermark (no commit)."""
    state = get_import_state(session, source, source_id)
    if state is None:
        state = ImportState(source=source, source_id=source_id)
        session.add(state)
    state.thread_id = thread_id
    state.last_line_count = last_line_count
    state.last_file_size = last_file_size
    state.last_message_uuid = last_message_uuid
    state.last_import_at = datetime.now(timezone.utc)
    return state
