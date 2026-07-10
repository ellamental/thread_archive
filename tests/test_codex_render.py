"""Codex threads must *read* like conversations.

The importer preserves every unmodeled codex line verbatim (test_preserve_codex);
these tests pin the other half of that bargain — the reader hides the machinery, drops
codex's duplicate ``response_item.message`` transcript, and renders what's left as text
rather than a JSON smash.
"""

from __future__ import annotations

import json

from thread_archive.importers import import_codex_session_incremental
from thread_archive.retrieval.read import read_thread, read_thread_structured
from thread_archive.store import init_db


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _session(archive_home, lines, source_id="codex-render"):
    init_db()
    f = archive_home / f"{source_id}.jsonl"
    _write_jsonl(f, lines)
    r = import_codex_session_incremental(f, source_id)
    return r.thread_id


def _blocks(thread_id, **kw):
    """Every rendered block as ``(role, type, block_type, text)``."""
    data = read_thread_structured(thread_id, **kw)
    return [
        (m["role"], b["type"], b.get("block_type"), b.get("text"))
        for m in data["messages"] for b in m["blocks"]
    ]


# The line stream codex actually writes: an API-history copy of every turn, wrapped in
# turn lifecycle and token telemetry, alongside the canonical event_msg stream.
def _realistic_lines() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "/proj", "model": "gpt-5"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "task_started", "turn_id": "t1", "model_context_window": 258400}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:01Z",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "<permissions>be careful</permissions>"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "fix the bug"}]}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
         "payload": {"type": "user_message", "message": "fix the bug", "turn_id": "t1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:04Z",
         "payload": {"type": "token_count", "info": {"total_tokens": 42}}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "agent_message", "message": "fixed it"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "fixed it"}]}},
        {"type": "event_msg", "timestamp": "2026-01-01T10:00:06Z",
         "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "fixed it"}},
    ]


def test_codex_transcript_copy_is_not_rendered_twice(archive_home) -> None:
    """Codex records each turn twice — ``event_msg`` (modeled) and ``response_item.message``
    (preserved). Only the modeled one renders, or every reply reads double."""
    thread_id = _session(archive_home, _realistic_lines())

    texts = [t for role, kind, _bt, t in _blocks(thread_id) if kind == "text" and role == "assistant"]
    assert texts == ["fixed it"], "the agent's reply should render exactly once"

    labels = [bt for _r, kind, bt, _t in _blocks(thread_id) if kind == "content_block"]
    assert "user message" not in labels, "the user's own words re-rendered as a preserved block"
    assert "assistant message" not in labels, "the reply re-rendered as a preserved block"


def test_codex_machinery_is_hidden(archive_home) -> None:
    """Turn lifecycle and token telemetry carry no conversation — they stay in the event
    log, out of the transcript."""
    thread_id = _session(archive_home, _realistic_lines())
    rendered = " ".join(str(t) for *_head, t in _blocks(thread_id))
    assert "task_started" not in rendered
    assert "task_complete" not in rendered
    assert "total_tokens" not in rendered
    assert "258400" not in rendered


def test_codex_developer_prompt_renders_as_text(archive_home) -> None:
    """The system prompt has no ``event_msg`` twin: it's the one ``response_item.message``
    that is real content, and it renders as its text, not as a JSON dump."""
    thread_id = _session(archive_home, _realistic_lines())
    blocks = [b for b in _blocks(thread_id) if b[2] == "developer message"]
    assert blocks == [
        ("assistant", "content_block", "developer message", "<permissions>be careful</permissions>")
    ]


def test_codex_injected_user_context_survives(archive_home) -> None:
    """Codex injects environment/instruction context wearing the user's role. It has no
    ``event_msg`` twin, so it isn't an echo — it must still be visible."""
    lines = _realistic_lines()
    lines.insert(3, {"type": "response_item", "timestamp": "2026-01-01T10:00:01Z",
                     "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "<environment_context>zsh</environment_context>"}]}})
    thread_id = _session(archive_home, lines, source_id="codex-injected")
    assert ("assistant", "content_block", "user message",
            "<environment_context>zsh</environment_context>") in _blocks(thread_id)


def test_codex_unknown_kind_renders_as_readable_json(archive_home) -> None:
    """A future codex line type stays visible, pretty-printed — never a one-line smash of
    every value in the block wrapper."""
    lines = _realistic_lines()
    lines.insert(-1, {"type": "response_item", "timestamp": "2026-01-01T10:00:05Z",
                      "payload": {"type": "some_future_kind", "detail": "hello"}})
    thread_id = _session(archive_home, lines, source_id="codex-future")
    match = [b for b in _blocks(thread_id) if b[2] == "some future kind"]
    assert match, "an unmodeled codex kind vanished from the transcript"
    text = match[0][3]
    assert json.loads(text)["detail"] == "hello"
    assert "response_item" not in text, "the block wrapper leaked into the rendered text"


def test_codex_web_search_renders_its_query(archive_home) -> None:
    lines = _realistic_lines()
    lines.insert(-1, {"type": "response_item", "timestamp": "2026-01-01T10:00:05Z",
                      "payload": {"type": "web_search_call", "status": "completed",
                                  "action": {"type": "search", "query": "python asyncio"}}})
    thread_id = _session(archive_home, lines, source_id="codex-search")
    assert ("assistant", "content_block", "web search",
            "search · python asyncio") in _blocks(thread_id)


def test_codex_hidden_blocks_stay_hidden_in_the_string_transcript(archive_home) -> None:
    """``read_thread`` (CLI / MCP) and the web viewer agree on what a codex thread says."""
    thread_id = _session(archive_home, _realistic_lines())
    text = read_thread(thread_id, mode="full")
    assert text.count("fixed it") == 1, "the reply is doubled in the agent-facing transcript"
    assert "task_started" not in text
    assert "[block: developer message] <permissions>be careful</permissions>" in text


def test_non_codex_preserved_blocks_are_untouched(archive_home) -> None:
    """The codex rules are keyed on the ``codex_`` block-type prefix — another provider's
    preserved block still renders under its own type."""
    from thread_archive.retrieval.read import _content_block_view

    payload = {"block_type": "redacted_thinking", "data": {"type": "redacted_thinking", "text": "hm"}}
    assert _content_block_view(payload, {"hm"}) == ("redacted_thinking", "hm")
