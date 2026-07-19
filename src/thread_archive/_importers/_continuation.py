"""Continuation / fork detection for the CC incremental import.

Decides whether a fresh Claude Code JSONL is a *continuation* of an existing
thread (merge into it) or a new/forked session (mint its own). Ported from
canonical ``streaming/incremental_import/_continuation.py``; the Postgres
``payload->>'content'`` becomes SQLite ``json_extract(payload, '$.content')``,
and the candidate lookup validates each same-first-message thread by the
prefix-consistency walk rather than disambiguating on a timestamp match.

Two detectors:

- ``detect_continuation_parent`` — the modern ``compact_boundary`` case: CC opens
  the post-compaction file with a boundary line + a user message pointing at the
  parent session's ``.jsonl`` path. Pull the parent UUID, resolve its thread.
- ``find_thread_by_first_message`` — older no-boundary compactions replay the whole
  conversation with fresh UUIDs (a strict prefix-superset of the parent). A *fork*
  shares a prefix then diverges; the prefix-consistency walk tells them apart.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Event
from ._state import lookup_parent_thread
from ._titles import _user_content_texts

if TYPE_CHECKING:
    from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

_PARENT_JSONL_RE = re.compile(r"/([0-9a-f-]{36})\.jsonl")


def detect_continuation_parent(
    session: Session, lines: list[dict], source_id: str
) -> Optional[str]:
    """If this is a ``compact_boundary`` continuation, return the parent thread_id."""
    has_boundary = any(line.get("subtype") == "compact_boundary" for line in lines[:3])
    if not has_boundary:
        return None

    project_prefix = source_id.rsplit(":", 1)[0]
    for line in lines[:5]:
        if line.get("type") != "user":
            continue
        for txt in _user_content_texts(line.get("message", {}).get("content", "")):
            match = _PARENT_JSONL_RE.search(txt)
            if match:
                parent_source_id = f"{project_prefix}:{match.group(1)}"
                return lookup_parent_thread(session, parent_source_id)
    return None


def find_thread_by_first_message(
    session: Session, lines: list[dict], parser: "ClaudeCodeParser", builder
) -> Optional[str]:
    """Find an existing thread this session is a no-boundary *continuation* of.

    Match the first user message's **content + timestamp** (content-only over-merges
    distinct sessions that happen to share a first message), then require prefix-
    consistency across the whole user-message overlap (a fork diverges → reject) and
    that the incoming session is at least as long as the candidate (a strict prefix is
    a not-yet-diverged fork → reject). Returns the thread to merge into, else None.

    The first message's ``occurred_at`` is taken from a probe build of that message —
    the deterministic parse of its source timestamp — so it matches the value the
    original import stored through the same column processor."""
    session_data = {
        "provider": "claude-code",
        "sessions": [{"session_id": "check", "project": "check", "lines": lines[:2000]}],
    }
    try:
        messages = parser.parse_export(session_data)
    except Exception:
        return None

    incoming_users = [
        m.get("content_text", "")
        for m in messages
        if m.get("role") == "user" and m.get("content_text")
    ]
    if not incoming_users:
        return None
    first_content = incoming_users[0]

    first_msg = next(
        (m for m in messages if m.get("role") == "user" and m.get("content_text") == first_content),
        None,
    )
    probe = builder.build_events(first_msg, "probe") if first_msg is not None else []
    anchor = next((e for e in probe if e.event_type == "user_message_sent"), None)
    if anchor is None:
        return None

    content_col = func.json_extract(Event.payload, "$.content")
    candidate = session.execute(
        select(Event.thread_id)
        .where(
            Event.event_type == "user_message_sent",
            content_col == first_content,
            Event.occurred_at == anchor.occurred_at,
        )
        .order_by(Event.id)
        .limit(1)
    ).scalar()
    if candidate is None:
        return None

    parent_users = session.execute(
        select(content_col)
        .where(Event.thread_id == candidate, Event.event_type == "user_message_sent")
        .order_by(Event.occurred_at, Event.id)
    ).scalars().all()
    # Prefix-consistency: any mismatch over the overlap → fork, not a continuation.
    if any(inc != par for inc, par in zip(incoming_users, parent_users)):
        return None
    # A real no-boundary continuation replays the parent's whole history then adds
    # more, so it's a superset; a strict prefix is a not-yet-diverged fork.
    if len(incoming_users) < len(parent_users):
        return None
    return candidate


def resolve_continuation_thread(
    session: Session, lines: list[dict], source_id: str, parser: "ClaudeCodeParser", builder
) -> Optional[str]:
    """The parent thread to merge this session into (boundary parent, else
    first-message superset), or None when it's a genuinely new/forked session."""
    parent = detect_continuation_parent(session, lines, source_id)
    if parent is not None:
        return parent
    return find_thread_by_first_message(session, lines, parser, builder)
