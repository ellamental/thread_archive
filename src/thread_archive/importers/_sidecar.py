"""Claude Code hook-context sidecar import.

Claude Code writes a sibling ``<session>.context.jsonl`` of hook-context lines
(``{"hook", "context", "ts", "prompt", "metadata"}``) — content that never reaches
the session JSONL. Each non-empty ``context`` line becomes a ``hook_context`` event
on the same thread, cursored independently via a derived ``{source_id}:context``
import-state row so re-imports only carry the grown tail. Ported from canonical
``streaming/incremental_import/_events.py:_import_sidecar_lines``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from thread_import.event_builder import compute_dedup_key

from ..store import Event
from ..truth import write_events
from ._events import _existing_dedup_keys
from ._read import read_session_lines
from ._state import get_import_state, upsert_import_state

logger = logging.getLogger(__name__)


def read_sidecar_lines(session_path) -> Optional[list[dict]]:
    """The sibling ``<session>.context.jsonl`` lines, or None when absent."""
    sidecar = Path(session_path).with_suffix(".context.jsonl")
    if not sidecar.exists():
        return None
    return read_session_lines(sidecar)


def import_sidecar_lines(
    session: Session, thread_id: int, source_id: str, sidecar_lines: list[dict]
) -> int:
    """Import grown hook-context sidecar lines as ``hook_context`` events. Returns
    the number written (idempotent: dedup_key membership + a line-count cursor)."""
    sidecar_source_id = f"{source_id}:context"
    state = get_import_state(session, "claude-code", sidecar_source_id)
    start_line = state.last_line_count if state else 0
    total = len(sidecar_lines)
    new_lines = sidecar_lines[start_line:]
    if not new_lines:
        return 0

    seen = _existing_dedup_keys(session, thread_id)
    batch: list[Event] = []
    for entry in new_lines:
        if not isinstance(entry, dict):
            continue
        context = entry.get("context", "")
        if not context:
            continue
        hook_name = entry.get("hook", "unknown")
        ts_str = entry.get("ts")
        prompt = entry.get("prompt")

        occurred_at: Optional[datetime] = None
        if ts_str:
            try:
                occurred_at = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass

        payload: dict = {"hook_name": hook_name, "context": context}
        if prompt:
            payload["prompt"] = prompt
        if entry.get("metadata"):
            payload["metadata"] = entry["metadata"]

        # Deterministic identity over the line's stable parts so a re-import collapses.
        content_anchor = f"{ts_str or ''}:{hook_name}:{context}"
        dedup_key = compute_dedup_key(content_anchor, "hook_context", payload)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        batch.append(Event(
            thread_id=thread_id,
            stream_id=str(uuid.uuid4()),
            event_type="hook_context",
            payload=payload,
            occurred_at=occurred_at or datetime.now(),
            dedup_key=dedup_key,
        ))

    if batch:
        write_events(session, batch)
        from ..retrieval.fts import index_events

        index_events(session, batch)

    upsert_import_state(
        session,
        source="claude-code",
        source_id=sidecar_source_id,
        thread_id=thread_id,
        last_line_count=total,
        last_file_size=0,
        last_message_uuid=None,
    )
    return len(batch)
