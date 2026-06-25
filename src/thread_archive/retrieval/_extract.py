"""Searchable-content extraction: an event's payload → FTS shadow rows.

Reads the payload dict keys directly — the importer's ``DefaultEventBuilder``
produces exactly these payload shapes, so there's no dependency on a dataclass
event-type hierarchy.

Each extractor returns ``(content, content_type, tool_name)`` tuples; an event
with no searchable content returns ``[]``.
"""

from __future__ import annotations

import json
from typing import Optional

# Event types that carry searchable content (must match the dispatch below).
#
# NB: ``api_request_completed`` is deliberately *not* indexed. thread-archive never
# live-streams — every assistant turn is *imported* via DefaultEventBuilder, which
# emits the granular text_complete / thinking_complete events alongside an
# api_request_completed summary that duplicates them. Indexing both double-counts
# the same text, so we index the granular events and drop the summary. (A
# live-streaming path would need the summary, since its stream emits text_delta
# rather than text_complete — but that path doesn't apply here.)
INDEXABLE_EVENT_TYPES = [
    "user_message_sent",
    "text_complete",
    "thinking_complete",
    "tool_use_started",
    "tool_use_complete",
    "tool_execution_completed",
    "context_summary",
    "tool_execution_error",
    "thread_message_sent",
]


def _to_str(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _strip_frontmatter(text: str) -> str:
    """Strip YAML frontmatter (---\\n...\\n---) from markdown content."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip()
    return text


def _fts_tool_use(payload: dict) -> list[tuple[str, str, Optional[str]]]:
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("input")
    if tool_name.startswith("mcp__") and isinstance(tool_input, dict) and "command" in tool_input:
        # Heredoc bodies span many lines; index the first line, truncated.
        tool_name = tool_input["command"].split("\n", 1)[0][:200]

    # Prioritize the real content keys over stringified metadata.
    content_parts = [payload.get("tool_name") or ""]
    if isinstance(tool_input, dict):
        for key in ("content", "text", "body", "message", "query", "prompt", "description", "entry"):
            val = tool_input.get(key)
            if val and isinstance(val, str):
                content_parts.append(_strip_frontmatter(val))
                break
        else:
            content_parts.append(" ".join(f"{k}={v}" for k, v in tool_input.items()))
    elif tool_input is not None:
        content_parts.append(str(tool_input))

    content = " ".join(content_parts).strip()
    return [(content[:2000], "tool", tool_name or None)] if content else []


def _fts_tool_completed(payload: dict) -> list[tuple[str, str, Optional[str]]]:
    output = payload.get("output")
    tool_name = payload.get("tool_name")
    if payload.get("is_error"):
        error_text = output or payload.get("error", "")
        return [(_to_str(error_text), "tool_error", tool_name)] if error_text else []
    if output:
        return [(_to_str(output), "tool_result", tool_name)]
    return []


def extract_fts_content(event_type: str, payload: dict) -> list[tuple[str, str, Optional[str]]]:
    """Extract searchable ``(content, content_type, tool_name)`` tuples from an event."""
    if not payload:
        return []

    if event_type == "user_message_sent":
        content = payload.get("content", "")
        if not content:
            return []
        if content.lstrip().startswith("This session is being continued from a previous conversation"):
            return [(content, "continuation_summary", None)]
        return [(content, "user", None)]

    if event_type == "api_request_completed":
        results: list[tuple[str, str, Optional[str]]] = []
        for block in payload.get("content_blocks", []) or []:
            bt = block.get("type")
            if bt == "thinking" and block.get("thinking"):
                results.append((block["thinking"], "thinking", None))
            elif bt == "text" and block.get("text"):
                results.append((block["text"], "text", None))
        return results

    if event_type == "text_complete":
        text = payload.get("text", "")
        return [(text, "text", None)] if text else []

    if event_type == "thinking_complete":
        text = payload.get("text", "")
        return [(text, "thinking", None)] if text else []

    if event_type in ("tool_use_started", "tool_use_complete"):
        return _fts_tool_use(payload)

    if event_type == "tool_execution_completed":
        return _fts_tool_completed(payload)

    if event_type == "tool_execution_error":
        error = payload.get("error", "")
        return [(_to_str(error), "tool_error", payload.get("tool_name"))] if error else []

    if event_type == "context_summary":
        content = payload.get("content", "")
        return [(content[:2000], "context_summary", None)] if content else []

    if event_type == "thread_message_sent":
        content = payload.get("content", "")
        return [(content, "user", None)] if content else []

    return []
