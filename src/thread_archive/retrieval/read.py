"""thread_read: reconstruct a readable conversation transcript from events.

The event log is the source of truth; a conversation is rebuilt by walking a
thread's events in order. The importer's ``DefaultEventBuilder`` emits granular
per-block events (text_complete / thinking_complete / tool_use_complete /
tool_execution_*) plus lifecycle/summary events (api_request_*, stream_completed)
— we render from the granular events and skip the lifecycle ones, grouping
contiguous assistant-side output under one ASSISTANT header.

This is the lean reconstruction; branch handling, MESSAGE_CORRECTED overlays, and
compaction boundaries are deferred.
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..store import Event, Thread, use_session

# Lifecycle / duplicate-summary events that carry no standalone transcript text.
_SKIP_TYPES = frozenset({
    "api_request_started",
    "api_request_completed",  # its content is already in text_complete/thinking_complete
    "stream_completed",
    "tool_loaded",
})


def _payload(ev: Event) -> dict:
    return ev.payload if isinstance(ev.payload, dict) else json.loads(ev.payload)


def _render_event(ev: Event, *, include_thinking: bool, include_tools: bool) -> Optional[tuple[str, str]]:
    """Return ``(role, text)`` for a renderable event, or None to skip it.

    role is 'user' or 'assistant' (drives the section header).
    """
    et = ev.event_type
    if et in _SKIP_TYPES:
        return None
    p = _payload(ev)

    if et == "user_message_sent":
        content = p.get("content", "")
        return ("user", content) if content.strip() else None
    if et == "thread_message_sent":
        content = p.get("content", "")
        return ("user", content) if content.strip() else None
    if et == "text_complete":
        text = p.get("text", "")
        return ("assistant", text) if text.strip() else None
    if et == "thinking_complete":
        if not include_thinking:
            return None
        text = p.get("text", "")
        return ("assistant", f"[thinking] {text}") if text.strip() else None
    if et in ("tool_use_complete", "tool_use_started"):
        if not include_tools:
            return None
        name = p.get("tool_name", "unknown")
        tool_input = p.get("input", {})
        return ("assistant", f"[tool: {name}] {json.dumps(tool_input, default=str)[:500]}")
    if et == "tool_execution_completed":
        if not include_tools:
            return None
        out = p.get("output", "")
        return ("assistant", f"[result] {str(out)[:500]}")
    if et == "tool_execution_error":
        if not include_tools:
            return None
        return ("assistant", f"[tool error] {str(p.get('error', ''))[:500]}")
    if et == "context_summary":
        content = p.get("content", "")
        return ("assistant", f"[context summary] {content}") if content.strip() else None
    return None


def read_thread(
    thread_id: int,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
    include_thinking: bool = False,
    include_tools: bool = True,
    session: Optional[Session] = None,
) -> str:
    """Reconstruct a thread as a readable transcript. Returns a message if the
    thread doesn't exist."""
    with use_session(session) as s:
        thread = s.get(Thread, thread_id)
        if thread is None:
            return f"Thread {thread_id} not found."
        events = s.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()

    header = f"# {thread.title or thread.name}"
    meta = f"thread {thread.id} · {thread.source or 'unknown'}"
    out: list[str] = [header, meta, ""]

    rendered = [
        r for r in (
            _render_event(ev, include_thinking=include_thinking, include_tools=include_tools)
            for ev in events
        )
        if r is not None
    ]
    window = rendered[offset:] if limit is None else rendered[offset:offset + limit]

    current_role: Optional[str] = None
    for role, text in window:
        if role != current_role:
            out.append("")
            out.append("## USER" if role == "user" else "## ASSISTANT")
            current_role = role
        out.append(text)

    if not window:
        out.append("_(no renderable content)_")
    return "\n".join(out)


# Cap a single tool result so a megabyte of command output never ships to the
# viewer whole; the UI shows the head and flags the truncation.
_TOOL_OUTPUT_CAP = 20_000


def _structured_event(
    ev: Event, *, include_thinking: bool, include_tools: bool
) -> Optional[tuple[str, dict]]:
    """Like :func:`_render_event`, but returns ``(role, block)`` where ``block`` is a
    typed renderable dict — so the web viewer can render markdown, syntax-highlighted
    code, and collapsible tool calls instead of a pre-flattened string. Mirrors the
    same event-type handling; returns None to skip lifecycle/empty events."""
    et = ev.event_type
    if et in _SKIP_TYPES:
        return None
    p = _payload(ev)

    if et in ("user_message_sent", "thread_message_sent"):
        content = p.get("content", "")
        return ("user", {"type": "text", "text": content}) if content.strip() else None
    if et == "text_complete":
        text = p.get("text", "")
        return ("assistant", {"type": "text", "text": text}) if text.strip() else None
    if et == "thinking_complete":
        if not include_thinking:
            return None
        text = p.get("text", "")
        return ("assistant", {"type": "thinking", "text": text}) if text.strip() else None
    if et in ("tool_use_complete", "tool_use_started"):
        if not include_tools:
            return None
        return ("assistant", {
            "type": "tool_use",
            "name": p.get("tool_name", "unknown"),
            "input": p.get("input", {}),
        })
    if et == "tool_execution_completed":
        if not include_tools:
            return None
        out = str(p.get("output", ""))
        return ("assistant", {
            "type": "tool_result",
            "output": out[:_TOOL_OUTPUT_CAP],
            "truncated": len(out) > _TOOL_OUTPUT_CAP,
        })
    if et == "tool_execution_error":
        if not include_tools:
            return None
        return ("assistant", {"type": "tool_error", "error": str(p.get("error", ""))})
    if et == "context_summary":
        content = p.get("content", "")
        return ("assistant", {"type": "context_summary", "text": content}) if content.strip() else None
    return None


def read_thread_structured(
    thread_id: int,
    *,
    include_thinking: bool = True,
    include_tools: bool = True,
    session: Optional[Session] = None,
) -> dict:
    """Reconstruct a thread as structured messages for the web viewer.

    Returns ``{thread_id, title, source, messages}`` where ``messages`` is a list of
    ``{role, blocks}`` — contiguous same-role events grouped into one message, each
    block a typed dict (:func:`_structured_event`). The string :func:`read_thread`
    stays the canonical transcript (CLI / MCP); this is the render-friendly sibling."""
    with use_session(session) as s:
        thread = s.get(Thread, thread_id)
        if thread is None:
            return {"thread_id": thread_id, "title": None, "source": None, "messages": []}
        events = s.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()

    messages: list[dict] = []
    current: Optional[dict] = None
    for ev in events:
        rendered = _structured_event(ev, include_thinking=include_thinking, include_tools=include_tools)
        if rendered is None:
            continue
        role, block = rendered
        if current is None or current["role"] != role:
            current = {"role": role, "blocks": []}
            messages.append(current)
        current["blocks"].append(block)

    return {
        "thread_id": thread.id,
        "title": thread.title or thread.name,
        "source": thread.source,
        "messages": messages,
    }
