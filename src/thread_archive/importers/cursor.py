"""Cursor (`state.vscdb`) DB scan + per-composer import + assembler.

A single live SQLite DB holds many conversations (composers + their bubbles). We
open it read-only, extract every composer, and run the per-composer importer
(incremental skip via the import_state cursor on ``lastUpdatedAt``). Pure
``_cursor_*`` / ``_build_cursor_messages`` helpers copied verbatim; orchestration
rewired onto our store + ``assemble_events``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from thread_import import DefaultEventBuilder

from ..store import ImportState, get_session
from ._events import assemble_events
from ._state import create_thread, get_import_state, get_thread_by_source, upsert_import_state

logger = logging.getLogger(__name__)


@dataclass
class CursorImportResult:
    events_created: int
    thread_id: int
    is_new_thread: bool


@dataclass
class CursorDbScanResult:
    composers_processed: int
    composers_imported: int
    events_created: int


def import_cursor_db(db_path) -> CursorDbScanResult:
    """Open a Cursor ``state.vscdb`` and import every composer in it."""
    import sqlite3

    db_path = Path(db_path)
    composers: dict[str, dict[str, Any]] = {}
    bubbles: dict[str, dict[str, Any]] = {}

    # Cursor's state.vscdb is live — open read-only (never lock a DB the editor
    # owns) with a busy timeout.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        ).fetchone():
            return CursorDbScanResult(0, 0, 0)

        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            try:
                composer_id = key.replace("composerData:", "")
                composers[composer_id] = json.loads(value)
            except (json.JSONDecodeError, KeyError):
                continue

        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ):
            try:
                parts = key.split(":")
                if len(parts) < 3:
                    continue
                composer_id, bubble_id = parts[1], parts[2]
                data = json.loads(value)
                data["_composerId"] = composer_id
                bubbles[f"{composer_id}:{bubble_id}"] = data
            except (json.JSONDecodeError, KeyError):
                continue
    finally:
        conn.close()

    summary = CursorDbScanResult(0, 0, 0)
    for composer_id, composer_data in composers.items():
        summary.composers_processed += 1
        try:
            composer_bubbles = {
                k: v for k, v in bubbles.items() if k.startswith(f"{composer_id}:")
            }
            result = import_cursor_from_payload(
                composer_id=composer_id, composer_data=composer_data, bubbles=composer_bubbles
            )
            if result.events_created > 0:
                summary.composers_imported += 1
                summary.events_created += result.events_created
        except Exception:
            logger.exception("import_cursor_db: composer %s failed; skipping", composer_id[:8])
    return summary


def import_cursor_from_payload(
    *, composer_id: str, composer_data: dict[str, Any], bubbles: dict[str, Any], session=None
) -> CursorImportResult:
    """Import one Cursor composer (atomic per-composer transaction when no session)."""
    if session is not None:
        return _run_cursor(session, composer_id, composer_data, bubbles)
    with get_session() as s:
        result = _run_cursor(s, composer_id, composer_data, bubbles)
        s.commit()
        return result


def _run_cursor(session, composer_id, composer_data, bubbles) -> CursorImportResult:
    source_id = composer_id
    import_state = get_import_state(session, "cursor", source_id)

    if _cursor_composer_unchanged(import_state, composer_data):
        return CursorImportResult(0, (import_state.thread_id or 0) if import_state else 0, False)

    messages = _build_cursor_messages(composer_id, composer_data, bubbles)
    if not messages:
        return CursorImportResult(0, 0, False)

    thread_id, is_new_thread = _cursor_resolve_thread(session, import_state, source_id, composer_data)

    start_index = import_state.last_line_count if import_state else 0
    new_messages = messages[start_index:]
    if not new_messages:
        return CursorImportResult(0, thread_id, is_new_thread)

    normalized = [_cursor_to_normalized(m) for m in new_messages]
    events_created, _ = assemble_events(session, thread_id, normalized, DefaultEventBuilder())
    upsert_import_state(
        session,
        source="cursor",
        source_id=source_id,
        thread_id=thread_id,
        last_line_count=len(messages),
        last_file_size=0,
        last_message_uuid=messages[-1].get("id") if messages else None,
    )
    return CursorImportResult(events_created, thread_id, is_new_thread)


def _cursor_composer_unchanged(import_state: Optional[ImportState], composer_data: dict[str, Any]) -> bool:
    if not (import_state and import_state.last_import_at):
        return False
    last_updated_ms = composer_data.get("lastUpdatedAt", 0)
    last_import_ms = import_state.last_import_at.timestamp() * 1000
    return last_updated_ms <= last_import_ms


def _cursor_resolve_thread(session, import_state, source_id, composer_data) -> tuple[int, bool]:
    if import_state and import_state.thread_id:
        return import_state.thread_id, False
    existing = get_thread_by_source(session, "cursor", source_id)
    if existing and existing.id is not None:
        return existing.id, False
    title = composer_data.get("name") or "Cursor Conversation"
    if len(title) > 100:
        title = title[:97] + "..."
    thread_id = create_thread(session, source="cursor", source_id=source_id, title=title)
    return thread_id, True


def _build_cursor_messages(
    composer_id: str, composer_data: dict[str, Any], all_bubbles: dict[str, Any]
) -> list[dict[str, Any]]:
    """Assemble Cursor composer + bubbles into the normalized message shape."""
    messages: list[dict[str, Any]] = []
    headers = composer_data.get("fullConversationHeadersOnly", [])
    if not headers:
        return messages

    for idx, header in enumerate(headers):
        if not isinstance(header, dict):
            continue
        bubble_id = header.get("bubbleId")
        if not bubble_id:
            continue
        bubble = all_bubbles.get(f"{composer_id}:{bubble_id}", {})
        bubble_type = bubble.get("type") or header.get("type")
        if bubble_type == 1:
            role = "user"
        elif bubble_type == 2:
            role = "assistant"
        else:
            role = "unknown"

        message: dict[str, Any] = {
            "id": bubble_id,
            "role": role,
            "content": bubble.get("text", ""),
            "created_at": bubble.get("createdAt"),
            "index": idx,
        }

        thinking = bubble.get("thinking")
        if thinking and isinstance(thinking, dict):
            thinking_text = thinking.get("text", "")
            if thinking_text:
                message["thinking"] = thinking_text

        tool_data = bubble.get("toolFormerData")
        if tool_data and isinstance(tool_data, dict):
            message["tool_call"] = {
                "name": tool_data.get("name", "unknown"),
                "tool_id": tool_data.get("tool"),
                "call_id": tool_data.get("toolCallId"),
                "status": tool_data.get("status"),
            }
        messages.append(message)
    return messages


def _cursor_iso(ts: Any) -> Optional[str]:
    if isinstance(ts, (int, float)):
        try:
            secs = ts / 1000 if ts > 1e12 else ts
            return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(ts, str) and ts:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def _cursor_to_normalized(msg: dict[str, Any]) -> dict[str, Any]:
    """Map one Cursor message into the canonical NormalizedMessage shape."""
    role = msg.get("role", "")
    created_at = _cursor_iso(msg.get("created_at"))
    pmid = msg.get("id", "")
    if role == "user":
        return {
            "role": "user",
            "created_at": created_at,
            "content_text": msg.get("content", ""),
            "content_blocks": [],
            "provider_message_id": pmid,
            "provider_data": {"provider": "cursor", "role": "user"},
        }
    if role == "assistant":
        blocks: list[dict[str, Any]] = []
        if msg.get("thinking"):
            blocks.append({"type": "thinking", "text": msg["thinking"]})
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        return {
            "role": "assistant",
            "created_at": created_at,
            "content_text": "",
            "content_blocks": blocks,
            "provider_message_id": pmid,
            "provider_data": {"provider": "cursor", "model": "cursor"},
        }
    return {"role": role}
