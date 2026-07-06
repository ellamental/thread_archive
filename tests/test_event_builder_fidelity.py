"""The builder must not silently drop content it doesn't specifically model.

Two fidelity holes fixed alongside the ide_context one:
  * unknown assistant **block types** (server_tool_use, web_search_tool_result,
    redacted_thinking, image, …) are preserved as ``content_block`` events, and
  * non-{user,assistant,system} **roles** (tool/function/developer/…) survive the
    shared assemble loop as ``message`` events instead of being dropped.

These matter because other harnesses emit shapes the Claude-Code path never does.
"""

from __future__ import annotations

from sqlalchemy import select

from thread_archive.importers._events import assemble_events
from thread_archive.importers._state import create_thread
from thread_archive.retrieval._extract import INDEXABLE_EVENT_TYPES, extract_fts_content
from thread_archive.store import Event, get_session, init_db
from thread_import import DefaultEventBuilder


def test_unknown_assistant_block_is_preserved() -> None:
    """An assistant block type the builder doesn't model is kept as a content_block
    event *and* round-trips verbatim in the api_request_completed summary."""
    b = DefaultEventBuilder()
    msg = {
        "role": "assistant",
        "provider_message_id": "a1",
        "created_at": "2026-01-01T10:00:00Z",
        "content_text": "",
        "content_blocks": [
            {"type": "text", "text": "searching the web"},
            {"type": "server_tool_use", "id": "srv1", "name": "web_search",
             "input": {"query": "eds joint pain"}},
        ],
        "provider_data": {"model": "m"},
    }
    events = b.build_events(msg, "stream", prev_occurred_at=None)

    cb = [e for e in events if e.event_type == "content_block"]
    assert len(cb) == 1
    assert cb[0].payload["block_type"] == "server_tool_use"
    assert cb[0].payload["data"]["input"]["query"] == "eds joint pain"
    # Anchored to its source message like every other event → not a phantom.
    assert cb[0].dedup_key.split(":", 1)[0] == "a1"

    # Verbatim in the api summary, so a re-export reproduces the original turn.
    completed = next(e for e in events if e.event_type == "api_request_completed")
    assert any(bl.get("type") == "server_tool_use" for bl in completed.payload["content_blocks"])


def test_unknown_assistant_block_is_searchable() -> None:
    """The preserved block is indexed by its human text, not its binary payload."""
    assert "content_block" in INDEXABLE_EVENT_TYPES
    rows = extract_fts_content(
        "content_block",
        {"block_type": "server_tool_use",
         "data": {"type": "server_tool_use", "name": "web_search",
                  "input": {"query": "eds joint pain"}}},
    )
    assert rows and "eds joint pain" in rows[0][0]
    # An image block's base64 source is skipped, not dumped into the index.
    img = extract_fts_content(
        "content_block",
        {"block_type": "image",
         "data": {"type": "image", "source": {"type": "base64", "data": "A" * 5000}}},
    )
    assert img == []


def test_unknown_role_is_preserved_through_assemble(archive_home) -> None:
    """The shared assemble loop keeps turns whose role isn't user/assistant/system —
    e.g. a ``tool`` or ``developer`` role from another harness — as message events."""
    init_db()
    b = DefaultEventBuilder()
    messages = [
        {"role": "user", "provider_message_id": "u1", "created_at": "2026-01-01T10:00:00Z",
         "content_text": "hi", "content_blocks": []},
        {"role": "tool", "provider_message_id": "t1", "created_at": "2026-01-01T10:00:01Z",
         "content_text": "plugin output", "content_blocks": []},
        {"role": "developer", "provider_message_id": "d1", "created_at": "2026-01-01T10:00:02Z",
         "content_text": "developer note", "content_blocks": []},
    ]
    with get_session() as s:
        tid = create_thread(s, source="test", source_id="s1")
        created, _ = assemble_events(s, tid, messages, b)
        s.commit()
    assert created >= 3  # nothing dropped

    with get_session() as s:
        events = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()

    msgs = {e.dedup_key.split(":", 1)[0]: e for e in events if e.event_type == "message"}
    assert set(msgs) == {"t1", "d1"}  # the two non-standard roles survived
    assert msgs["t1"].payload["role"] == "tool"
    assert msgs["t1"].payload["content"] == "plugin output"
    assert msgs["d1"].payload["role"] == "developer"
    # And the standard user turn still imported normally.
    assert any(e.event_type == "user_message_sent" for e in events)
