"""The grok importer must capture EVERYTHING — no provider record silently dropped.

Each test drives the real importer over a fixture ``chat_history.jsonl`` that contains
one of the droppable grok record shapes (system line, context-only user turn,
synthetic/injected user turn, unmodeled line type) and proves the record lands as
at least one event, with its content and raw line preserved. See the drop-site
guards in ``thread_archive._importers.grok``.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers import import_grok_session_incremental
from thread_archive._store import Event, get_session, init_db


def _write_grok_session(archive_home, lines) -> "object":
    """Write a grok ``chat_history.jsonl`` fixture and return its path."""
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    path = session_dir / "chat_history.jsonl"
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    return path


def _events() -> list[tuple[str, dict]]:
    with get_session() as s:
        return [(e.event_type, e.payload) for e in s.execute(select(Event)).scalars().all()]


def test_grok_system_and_unknown_lines_preserved(archive_home) -> None:
    """A ``system`` line and a novel/unmodeled line type both survive import as
    events carrying their text and raw line (never silently dropped)."""
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
        {"type": "system", "content": "You are grok. Follow the SYSTEM PROMPT closely."},
        {"type": "assistant", "content": "hello", "tool_calls": []},
        {"type": "checkpoint", "content": "a NOVEL grok line type payload"},
    ])

    r = import_grok_session_incremental(f, "grok-sess")
    assert r.is_new_thread and r.events_created > 0
    events = _events()

    # System line preserved with its text + raw line.
    system_ev = next(
        (p for t, p in events if t == "context_summary" and "SYSTEM PROMPT" in (p.get("content") or "")),
        None,
    )
    assert system_ev is not None, "grok system line was dropped"
    assert system_ev["provider_data"]["raw_line"]["type"] == "system"

    # Unmodeled line type preserved with its text + raw line.
    unknown_ev = next(
        (p for t, p in events if t == "context_summary" and "NOVEL grok line type" in (p.get("content") or "")),
        None,
    )
    assert unknown_ev is not None, "unmodeled grok line type was dropped"
    assert unknown_ev["provider_data"]["raw_line"]["type"] == "checkpoint"

    # Idempotent re-import adds nothing.
    n = len(events)
    r2 = import_grok_session_incremental(f, "grok-sess")
    assert r2.events_created == 0
    assert len(_events()) == n


def test_grok_user_context_is_not_stripped_or_dropped(archive_home) -> None:
    """The full user text is kept: text around ``<user_query>`` must not be
    discarded, and a context-only turn (no query span) must not be dropped."""
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "user", "content": [{"type": "text",
         "text": "<user_info>ella, plural</user_info><user_query>what is 2+2</user_query>"}]},
        {"type": "assistant", "content": "4", "tool_calls": []},
        {"type": "user", "content": [{"type": "text", "text": "<system-reminder>stay terse</system-reminder>"}]},
        {"type": "assistant", "content": "ok", "tool_calls": []},
    ])

    import_grok_session_incremental(f, "grok-sess")
    user_texts = [p.get("content") or "" for t, p in _events() if t == "user_message_sent"]

    # The wrapped turn keeps the injected context AND the query (nothing outside the
    # tags discarded).
    wrapped = next((c for c in user_texts if "what is 2+2" in c), None)
    assert wrapped is not None
    assert "<user_info>" in wrapped and "ella, plural" in wrapped

    # The context-only turn (no <user_query>) is preserved, not dropped.
    assert any("stay terse" in c for c in user_texts), "context-only user turn was dropped"


def test_grok_synthetic_user_turn_preserved_and_tagged(archive_home) -> None:
    """A synthetic/injected user turn is kept AND tagged with its reason + raw
    line rather than dropped."""
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "user", "content": [{"type": "text", "text": "<user_query>real question</user_query>"}]},
        {"type": "user", "synthetic_reason": "auto_continue",
         "content": [{"type": "text", "text": "Please continue where you left off."}]},
        {"type": "assistant", "content": "continuing", "tool_calls": []},
    ])

    import_grok_session_incremental(f, "grok-sess")

    synthetic_ev = next(
        (p for t, p in _events()
         if t == "context_summary" and "Please continue" in (p.get("content") or "")),
        None,
    )
    assert synthetic_ev is not None, "synthetic user turn was dropped"
    pd = synthetic_ev["provider_data"]
    assert pd["synthetic"] is True
    assert pd["synthetic_reason"] == "auto_continue"
    assert pd["raw_line"]["synthetic_reason"] == "auto_continue"
