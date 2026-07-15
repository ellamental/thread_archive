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
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.timestamps import parse_timestamp_iso

from .._store import ImportState, get_session
from ._events import assemble_events
from ._result import DbScanResult
from ._state import (
    create_thread,
    get_import_state,
    get_thread_by_source,
    last_import_epoch_ms,
    upsert_import_state,
)

logger = logging.getLogger(__name__)


@dataclass
class CursorImportResult:
    events_created: int
    thread_id: int
    is_new_thread: bool


def import_cursor_db(db_path) -> DbScanResult:
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
            return DbScanResult()

        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            composer_id = key.replace("composerData:", "")
            try:
                composers[composer_id] = json.loads(value)
            except (json.JSONDecodeError, TypeError) as e:
                # A corrupt composer blob must not vanish on a silent `continue`.
                # Log it and preserve a stub thread carrying the raw + error so a
                # failed conversation is visible in the archive, not silently lost.
                logger.warning(
                    "import_cursor_db: composer %s failed to parse; preserving stub: %s",
                    composer_id[:8], e,
                )
                try:
                    _import_cursor_error_stub(composer_id, value, {}, e)
                except Exception:
                    logger.exception(
                        "import_cursor_db: composer %s parse-stub failed", composer_id[:8]
                    )
                continue

        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ):
            parts = key.split(":")
            if len(parts) < 3:
                logger.warning("import_cursor_db: malformed bubble key %r; skipping", key)
                continue
            composer_id, bubble_id = parts[1], parts[2]
            try:
                data = json.loads(value)
                data["_composerId"] = composer_id
            except (json.JSONDecodeError, TypeError) as e:
                # A corrupt bubble must not vanish on a silent `continue`. Keep a raw
                # stub: if a header references it, the unknown-bubble path
                # (_cursor_to_normalized) preserves it as a `message` event.
                logger.warning(
                    "import_cursor_db: bubble %s failed to parse; keeping raw stub: %s", key, e
                )
                data = {
                    "_composerId": composer_id,
                    "_parse_error": str(e),
                    "_raw": value,
                    "type": None,
                }
            bubbles[f"{composer_id}:{bubble_id}"] = data
    finally:
        conn.close()

    summary = DbScanResult()
    for composer_id, composer_data in composers.items():
        summary.processed += 1
        composer_bubbles = {
            k: v for k, v in bubbles.items() if k.startswith(f"{composer_id}:")
        }
        try:
            result = import_cursor_from_payload(
                composer_id=composer_id, composer_data=composer_data, bubbles=composer_bubbles
            )
            if result.events_created > 0:
                summary.imported += 1
                summary.events_created += result.events_created
        except Exception as e:
            # One composer blowing up must not skip it silently. Log the traceback,
            # count it out to the watcher's health, AND preserve a stub thread carrying
            # its id + raw + error so the failed conversation stays visible in the
            # archive and re-exportable.
            logger.exception(
                "import_cursor_db: composer %s failed; preserving stub", composer_id[:8]
            )
            summary.failed += 1
            summary.errors.append(f"composer {composer_id[:8]}: {e}")
            try:
                _import_cursor_error_stub(composer_id, composer_data, composer_bubbles, e)
            except Exception:
                logger.exception(
                    "import_cursor_db: composer %s stub also failed", composer_id[:8]
                )
    return summary


def _import_cursor_error_stub(
    composer_id: str, composer_data: Any, bubbles: dict[str, Any], error: Exception
) -> None:
    """Preserve a composer that failed to load/import as its own stub thread.

    A corrupt (unparseable JSON) or blow-up composer must not vanish behind a
    silent ``continue`` on the load or a logged-and-dropped ``skipping`` on the
    import. Keep a stub thread carrying the raw payload + error under a distinct
    ``:import-error`` source id, so the failure is visible in the archive and
    re-exportable while the real composer stays unimported and retryable on the
    next scan (its ``import_state`` watermark never advanced). Idempotent via the
    builder's dedup_key.
    """
    stub_source_id = f"{composer_id}:import-error"
    raw: dict[str, Any] = {
        "composer_id": composer_id,
        "error": str(error),
        "composer_data": composer_data,
        "bubble_keys": sorted(bubbles.keys()),
    }
    message = {
        "role": "cursor_import_error",
        "created_at": None,
        "content_text": f"[cursor import failed for {composer_id}: {error}]",
        "content_blocks": [{"type": "cursor_raw", "data": raw}],
        "provider_message_id": stub_source_id,
        "provider_data": {"provider": "cursor", "role": "unknown", "import_error": str(error)},
    }
    with get_session() as s:
        existing = get_thread_by_source(s, "cursor", stub_source_id)
        if existing and existing.id is not None:
            thread_id = existing.id
        else:
            name = composer_data.get("name") if isinstance(composer_data, dict) else None
            title = f"{name or 'Cursor Conversation'} (import error)"
            if len(title) > 100:
                title = title[:97] + "..."
            thread_id = create_thread(s, source="cursor", source_id=stub_source_id, title=title)
        assemble_events(s, thread_id, [message], DefaultEventBuilder())
        s.commit()


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
    # cross_pass_dedup: a full re-scan of an already-imported composer (import_state
    # reset, or a manual re-run) must be a no-op even against rows the dedup_key
    # can't see — a bulk-seeded row can carry a NULL dedup_key, so keying only on it
    # would let a re-import stack duplicate events. The message-level (content +
    # timestamp) existence check skips any turn already present regardless of
    # dedup_key, mirroring the claude-code continuation guard.
    events_created, _ = assemble_events(
        session, thread_id, normalized, DefaultEventBuilder(), cross_pass_dedup=True
    )
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
    return last_updated_ms <= last_import_epoch_ms(import_state)


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
        # An unmodeled bubble type keeps its raw payload + resolved type so the
        # normalized mapping can preserve the whole turn (content/thinking/tool/raw)
        # rather than drop it — see _cursor_to_normalized's unknown branch.
        if role == "unknown":
            message["bubble_type"] = bubble_type
            if bubble:
                message["raw"] = bubble

        thinking = bubble.get("thinking")
        if thinking and isinstance(thinking, dict):
            thinking_text = thinking.get("text", "")
            if thinking_text:
                message["thinking"] = thinking_text

        tool_data = bubble.get("toolFormerData")
        if tool_data and isinstance(tool_data, dict):
            # ``params`` is Cursor's resolved arg dict; ``rawArgs`` the model's raw
            # JSON-string args. Prefer params, fall back to parsed rawArgs. ``result``
            # is the tool output. Capturing input+result lets us emit the tool_use +
            # tool_execution events the builder produces — dropping them would lose
            # every tool call on a re-parse.
            tool_input = tool_data.get("params")
            if tool_input is None:
                raw = tool_data.get("rawArgs")
                if isinstance(raw, str):
                    try:
                        tool_input = json.loads(raw)
                    except json.JSONDecodeError:
                        tool_input = raw
                else:
                    tool_input = raw
            message["tool_call"] = {
                "name": tool_data.get("name", "unknown"),
                "tool_id": tool_data.get("tool"),
                "call_id": tool_data.get("toolCallId"),
                "status": tool_data.get("status"),
                "input": tool_input,
                "result": tool_data.get("result"),
            }
        messages.append(message)
    return messages


def _cursor_iso(ts: Any) -> Optional[str]:
    # Cursor stores either epoch-ms numbers or ISO strings; both normalize to the
    # canonical aware-UTC ISO form (the ms/1e12 threshold and offset-less→UTC
    # coercion live in parse_timestamp).
    return parse_timestamp_iso(ts)


def _cursor_tool_blocks(tc: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_use (+ tool_result) blocks for a Cursor ``tool_call``. Shared by the
    assistant and unknown-bubble mappings so a tool call is preserved either way."""
    call_id = tc.get("call_id")
    name = tc.get("name", "unknown")
    blocks: list[dict[str, Any]] = [{
        "type": "tool_use",
        "tool_call_id": call_id,
        "name": name,
        "input": tc.get("input") or {},
    }]
    result = tc.get("result")
    if result is not None:
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        blocks.append({
            "type": "tool_result",
            "tool_use_id": call_id,
            "name": name,
            "content": content,
            "is_error": tc.get("status") == "error",
        })
    return blocks


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
        tc = msg.get("tool_call")
        if tc and isinstance(tc, dict):
            blocks.extend(_cursor_tool_blocks(tc))
        return {
            "role": "assistant",
            "created_at": created_at,
            "content_text": "",
            "content_blocks": blocks,
            "provider_message_id": pmid,
            "provider_data": {"provider": "cursor", "model": "cursor"},
        }
    # An unmodeled bubble type. The shared builder preserves any non-user/assistant/
    # system role as a `message` event (role + content + content_blocks), so carry the
    # bubble's content/thinking/tool_call + full raw here rather than dropping the turn —
    # the old bare {"role": role} silently discarded every one.
    role = role or "unknown"
    unknown_blocks: list[dict[str, Any]] = []
    if msg.get("thinking"):
        unknown_blocks.append({"type": "thinking", "text": msg["thinking"]})
    tc = msg.get("tool_call")
    if tc and isinstance(tc, dict):
        unknown_blocks.extend(_cursor_tool_blocks(tc))
    provider_data: dict[str, Any] = {"provider": "cursor", "role": role}
    raw = msg.get("raw")
    if raw:
        provider_data["raw"] = raw
        unknown_blocks.append(
            {"type": "cursor_raw", "bubble_type": msg.get("bubble_type"), "data": raw}
        )
    return {
        "role": role,
        "created_at": created_at,
        "content_text": msg.get("content", ""),
        "content_blocks": unknown_blocks,
        "provider_message_id": pmid,
        "provider_data": provider_data,
    }
