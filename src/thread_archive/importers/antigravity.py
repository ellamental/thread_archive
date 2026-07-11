"""Antigravity (Gemini CLI) incremental session import + assembler.

Pure ``_antigravity_*`` / ``_build_antigravity_messages`` helpers; orchestration
wired onto the shared scaffold.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder

from ._events import assemble_events
from ._line_stream import import_line_stream_session
from ._result import IncrementalImportResult

logger = logging.getLogger(__name__)


def import_antigravity_session_incremental(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
    """Import an Antigravity (Gemini agentic CLI) transcript.jsonl into the event log."""

    def _do_import(sess, thread_id, all_lines, new_lines, _ctx):
        messages = _build_antigravity_messages(new_lines, all_lines, source_id)
        return assemble_events(sess, thread_id, messages, DefaultEventBuilder())

    return import_line_stream_session(
        source="antigravity",
        source_id=source_id,
        session_path=session_path,
        session=session,
        not_found_msg=f"Antigravity session file not found: {session_path}",
        has_importable_content=_antigravity_has_importable_content,
        make_title=lambda all_lines, _ctx: _antigravity_title(all_lines),
        import_lines=_do_import,
    )


_ANTIGRAVITY_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_ANTIGRAVITY_MODEL_RE = re.compile(r"changed setting `Model Selection` from .*? to (.+?)\.\s")
# MODEL step `type` values that are the model's natural-language turn; everything
# else a MODEL step emits is a tool action/result.
_ANTIGRAVITY_TEXT_TYPES = {"PLANNER_RESPONSE"}


def _antigravity_kind(line: dict) -> Optional[str]:
    source = line.get("source")
    line_type = line.get("type")
    if source == "USER_EXPLICIT" or line_type == "USER_INPUT":
        return "user"
    if source == "MODEL":
        return "assistant" if line_type in _ANTIGRAVITY_TEXT_TYPES else "tool_result"
    if line_type == "ERROR_MESSAGE":
        return "error"
    return None


def _antigravity_content(line: dict) -> str:
    content = line.get("content")
    if not isinstance(content, str) or not content.strip():
        return ""
    if _antigravity_kind(line) == "user":
        m = _ANTIGRAVITY_USER_REQUEST_RE.search(content)
        if m:
            return m.group(1).strip()
    return content.strip()


def _antigravity_tool_args(args: Any) -> dict[str, Any]:
    """Antigravity tool_call args arrive double-JSON-encoded; unwrap each string once."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out


def _antigravity_model(lines: list[dict]) -> str:
    model = "gemini"
    for line in lines:
        content = line.get("content")
        if not isinstance(content, str):
            continue
        m = _ANTIGRAVITY_MODEL_RE.search(content)
        if m:
            model = m.group(1).strip()[:80]
    return model


def _antigravity_has_importable_content(lines: list[dict]) -> bool:
    for line in lines:
        kind = _antigravity_kind(line)
        if kind == "user" and _antigravity_content(line):
            return True
        if kind == "assistant" and (_antigravity_content(line) or line.get("tool_calls")):
            return True
    return False


def _antigravity_title(lines: list[dict]) -> str:
    for line in lines:
        if _antigravity_kind(line) == "user":
            text_content = _antigravity_content(line)
            if text_content:
                first = next((part.strip() for part in text_content.splitlines() if part.strip()), "")
                return (first[:97] + "...") if len(first) > 100 else first
    return "Antigravity Session"


def _antigravity_preserved_message(line: dict, ts: Optional[str]) -> dict[str, Any]:
    """Preserve an antigravity step the importer doesn't model, rather than drop it.

    ``_antigravity_kind`` only maps user/model/error steps; any other step (a
    ``Model Selection`` setting change, a system notice, a future step kind) returns
    None and would be skipped. Kept here as a ``role="unknown"`` message so the
    builder emits a ``message`` event carrying the step's text plus the raw step
    verbatim under ``content_blocks`` — nothing silently lost, and idempotent because
    the raw step is inside the dedup-hashed ``content_blocks``."""
    content = line.get("content")
    text = content.strip() if isinstance(content, str) else ""
    return {
        "role": "unknown",
        "created_at": ts,
        "content_text": text,
        "content_blocks": [{
            "type": "antigravity_step",
            "source": line.get("source"),
            "step_type": line.get("type"),
            "raw": line,
            "start_timestamp": ts,
        }],
        "provider_message_id": ts or "",
        "provider_data": {"provider": "antigravity"},
    }


def _build_antigravity_messages(
    new_lines: list[dict],
    all_lines: list[dict],
    source_id: str,
) -> list[dict[str, Any]]:
    """Assemble antigravity's transcript steps into canonical NormalizedMessages.

    Tool calls carry no id and their result arrives as a later step, so each
    PLANNER_RESPONSE turn mints a deterministic id per tool_use (``agtool-<n>`` by
    document order, stable across re-imports so the dedup key holds), and each
    following tool/error step is paired FIFO and folded back as a tool_result.
    """
    messages: list[dict[str, Any]] = []
    model = _antigravity_model(all_lines)
    cur: Optional[dict[str, Any]] = None
    pending: list[tuple[str, Optional[str], dict[str, Any]]] = []
    tool_seq = 0

    def new_assistant(ts: Optional[str]) -> dict[str, Any]:
        turn = {
            "role": "assistant",
            "created_at": ts,
            "content_text": "",
            "content_blocks": [],
            "provider_message_id": ts or "",
            "provider_data": {"provider": "antigravity", "model": model},
        }
        messages.append(turn)
        return turn

    def handle_assistant(line: dict, ts: Optional[str], content: str) -> None:
        nonlocal cur, tool_seq
        tool_calls = [tc for tc in (line.get("tool_calls") or []) if isinstance(tc, dict)]
        if not content and not tool_calls:
            return
        cur = new_assistant(ts)
        if content:
            cur["content_blocks"].append({"type": "text", "text": content, "start_timestamp": ts})
        for tc in tool_calls:
            tid = f"agtool-{tool_seq}"
            tool_seq += 1
            name = tc.get("name") or "unknown"
            cur["content_blocks"].append({
                "type": "tool_use", "id": tid, "name": name,
                "input": _antigravity_tool_args(tc.get("args")),
                "start_timestamp": ts,
            })
            pending.append((tid, name, cur))

    def handle_outcome(line: dict, kind: str, ts: Optional[str], content: str) -> None:
        # An empty outcome still records that a tool completed/errored — and dropping
        # it would leave its tool_use unpaired, so the NEXT outcome would mis-pair to
        # it FIFO. Keep it: pop the pending call (honest empty content), never drop.
        nonlocal tool_seq
        if pending:
            tid, call_name, owner = pending.pop(0)
        else:
            owner = cur if cur is not None else new_assistant(ts)
            tid, call_name = f"agorphan-{tool_seq}", None
            tool_seq += 1
        owner["content_blocks"].append({
            "type": "tool_result",
            "tool_use_id": tid,
            "name": call_name or str(line.get("type") or "tool").lower(),
            "content": content,
            "is_error": kind == "error",
            "start_timestamp": ts,
        })

    for line in new_lines:
        if not isinstance(line, dict):
            continue
        kind = _antigravity_kind(line)
        ts = line.get("created_at")

        if kind is None:
            # A step the importer doesn't model (setting change, system notice, a
            # future kind). Preserve it unless it's a genuinely-empty record with
            # nothing to keep.
            if line:
                messages.append(_antigravity_preserved_message(line, ts))
            continue

        content = _antigravity_content(line)

        if kind == "user":
            if not content:
                continue
            cur = None
            messages.append({
                "role": "user",
                "created_at": ts,
                "content_text": content,
                "content_blocks": [],
                "provider_message_id": ts or "",
                "provider_data": {"provider": "antigravity"},
            })
        elif kind == "assistant":
            handle_assistant(line, ts, content)
        else:
            handle_outcome(line, kind, ts, content)

    # Keep every user turn, plus any turn carrying SOME content (blocks or text);
    # only a truly-empty non-user turn (no blocks, no text) is dropped.
    return [
        m for m in messages
        if m["role"] == "user" or m["content_blocks"] or m.get("content_text", "").strip()
    ]
