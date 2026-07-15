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


def test_codex_tool_failures_import_as_errors(archive_home) -> None:
    """A tool output whose wrapper header carries a nonzero exit status must land
    as ``tool_execution_error``, not a success — but only on the unambiguous
    signals: a header ``Process exited with code N`` / ``Exit code: N`` line or a
    JSON output with integer ``exit_code``. An output merely quoting an exit-code
    line deep in its body stays a success."""
    init_db()
    f = archive_home / "codex.jsonl"

    def call(cid: str) -> dict:
        return {"type": "response_item", "timestamp": "2026-01-01T10:00:01Z",
                "payload": {"type": "function_call", "call_id": cid, "name": "shell",
                            "arguments": "{}"}}

    def result(cid: str, output: str) -> dict:
        return {"type": "response_item", "timestamp": "2026-01-01T10:00:02Z",
                "payload": {"type": "function_call_output", "call_id": cid, "output": output}}

    quoted_deep = "Wall time: 0.1 seconds\nOutput:\n" + "\n" * 5 + "Process exited with code 1"
    _write_jsonl(f, [
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "user_message", "message": "run stuff", "turn_id": "t1"}},
        call("c1"), result("c1", "Chunk ID: a\nWall time: 0.0 seconds\n"
                                 "Process exited with code 1\nOriginal token count: 5\n"
                                 "Output:\nzsh: command not found"),
        call("c2"), result("c2", "Chunk ID: b\nWall time: 0.0 seconds\n"
                                 "Process exited with code 0\nOriginal token count: 5\n"
                                 "Output:\nfine"),
        call("c3"), result("c3", quoted_deep),
        call("c4"), result("c4", '{"exit_code":2,"output":"boom"}'),
        call("c5"), result("c5", "Exit code: 1\nWall time: 0.2 seconds\nOutput:\nnope"),
    ])

    r = import_codex_session_incremental(f, "codex-errors")
    assert r.events_created > 0

    with get_session() as s:
        rows = s.execute(select(Event.event_type, Event.payload)).all()
    errored = {p["tool_call_id"] for t, p in rows if t == "tool_execution_error"}
    completed = {p["tool_call_id"] for t, p in rows if t == "tool_execution_completed"}
    assert errored == {"c1", "c4", "c5"}
    assert completed == {"c2", "c3"}


def _all_event_ids() -> list[int]:
    with get_session() as s:
        return [i for (i,) in s.execute(select(Event.id))]
