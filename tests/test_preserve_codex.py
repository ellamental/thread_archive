"""Codex importer must capture EVERYTHING: a response_item / event_msg kind the
importer doesn't model is preserved as a ``content_block`` event carrying the raw
payload, never silently dropped.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers import import_codex_session_incremental
from thread_archive._store import Event, get_session, init_db


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_codex_preserves_unknown_response_item(archive_home) -> None:
    """A ``web_search_call`` response_item (outside the modeled reasoning/function/
    tool set) must not return None and vanish: it must become a ``content_block``
    event that keeps the raw payload verbatim."""
    init_db()
    f = archive_home / "codex.jsonl"
    _write_jsonl(f, [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "/proj", "model": "gpt-5"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "search the web", "turn_id": "t1"}},
        # A response_item kind the importer does not model — must still be kept.
        {"type": "response_item", "timestamp": "2026-01-01T10:00:03Z",
         "payload": {"type": "web_search_call", "id": "ws1", "status": "completed",
                     "action": {"query": "python asyncio"}}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "agent_message", "message": "done"}},
    ])

    r = import_codex_session_incremental(f, "codex-preserve")
    assert r.is_new_thread and r.events_created > 0

    with get_session() as s:
        blocks = s.execute(
            select(Event.payload).where(Event.event_type == "content_block")
        ).scalars().all()
    assert blocks, "unknown codex response_item was dropped instead of preserved"

    match = [p for p in blocks if p.get("block_type") == "codex_web_search_call"]
    assert match, "preserved block lost its codex type"
    assert match[0]["data"]["raw"] == {
        "type": "web_search_call", "id": "ws1", "status": "completed",
        "action": {"query": "python asyncio"},
    }

    # Idempotent: the preserved event dedups on re-import.
    n = len(_all_event_ids())
    r2 = import_codex_session_incremental(f, "codex-preserve")
    assert r2.events_created == 0
    assert len(_all_event_ids()) == n


def test_codex_preserves_unknown_event_msg(archive_home) -> None:
    """An ``event_msg`` kind other than user_message/agent_message (here a
    ``token_count`` telemetry step) is preserved rather than dropped."""
    init_db()
    f = archive_home / "codex.jsonl"
    _write_jsonl(f, [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "hi", "turn_id": "t1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
         "payload": {"type": "token_count", "info": {"total_tokens": 42}}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "agent_message", "message": "hello"}},
    ])

    r = import_codex_session_incremental(f, "codex-evt")
    assert r.events_created > 0

    with get_session() as s:
        blocks = s.execute(
            select(Event.payload).where(Event.event_type == "content_block")
        ).scalars().all()
    types = {p.get("block_type") for p in blocks}
    assert "codex_token_count" in types, "unknown event_msg kind was dropped"


def _all_event_ids() -> list[int]:
    with get_session() as s:
        return [i for (i,) in s.execute(select(Event.id))]
