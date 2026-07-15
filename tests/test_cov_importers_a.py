"""Branch-coverage tests for the grok / codex / cursor importers.

Coverage tag: ``impa``. These drive the real importers over fixture transcripts
(and exercise the pure helpers directly) to reach the unmodeled-record,
tool-call, reasoning, multi-session-scan, model-extraction, continuation/dedup,
and error/skip branches that the existing preserve/provider suites leave
uncovered. Behavior is asserted against the current implementation, never
mutated.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select

import thread_archive._importers.cursor as cursor_mod
from thread_archive._importers import (
    import_codex_session_incremental,
    import_cursor_db,
    import_cursor_from_payload,
    import_grok_session_incremental,
)
from thread_archive._importers.codex import (
    _build_codex_messages,
    _codex_assistant_block,
    _codex_call_input,
    _codex_call_maps,
    _codex_first_user_line,
    _codex_has_importable_content,
    _codex_line_model,
    _codex_model,
    _codex_output_is_error,
    _codex_reasoning_text,
    _codex_title,
    _codex_tool_result_block,
    _codex_tool_use_block,
    _codex_user_message,
)
from thread_archive._importers.cursor import (
    _build_cursor_messages,
    _cursor_to_normalized,
    _cursor_tool_blocks,
)
from thread_archive._importers.grok import (
    _build_grok_messages,
    _grok_accumulate_thinking,
    _grok_build_assistant_message,
    _grok_extract_query,
    _grok_first_query,
    _grok_fold_tool_update,
    _grok_has_importable_content,
    _grok_model,
    _grok_pop_prompt_ts,
    _grok_prompt_ts_map,
    _grok_reasoning_text,
    _grok_session_meta,
    _grok_title,
    _grok_tool_input,
    _grok_tool_result_block,
    _grok_tool_timestamps,
    _grok_user_text,
    _harvest_tool_names,
)
from thread_archive._store import Event, Thread, get_session, init_db


def _events() -> list[tuple[str, dict]]:
    with get_session() as s:
        return [(e.event_type, e.payload) for e in s.execute(select(Event)).scalars()]


def _threads() -> dict[str, Thread]:
    with get_session() as s:
        return {t.source_id: t for t in s.execute(select(Thread)).scalars()}


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# GROK
# ══════════════════════════════════════════════════════════════════════════


def _write_grok(archive_home, chat_lines, *, summary=None, updates=None, prompts=None):
    """Write a grok session dir (chat_history + optional siblings). Returns path."""
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir(exist_ok=True)
    path = session_dir / "chat_history.jsonl"
    path.write_text("\n".join(json.dumps(ln) for ln in chat_lines) + "\n", encoding="utf-8")
    if summary is not None:
        (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if updates is not None:
        # updates may include raw strings (blank/bad lines) mixed with dicts.
        blob = "\n".join(u if isinstance(u, str) else json.dumps(u) for u in updates)
        (session_dir / "updates.jsonl").write_text(blob + "\n", encoding="utf-8")
    if prompts is not None:
        blob = "\n".join(p if isinstance(p, str) else json.dumps(p) for p in prompts)
        (archive_home / "prompt_history.jsonl").write_text(blob + "\n", encoding="utf-8")
    return path


def test_grok_full_session_tools_reasoning_and_siblings(archive_home) -> None:
    """A full grok session with reasoning, a tool round (with updates.jsonl times),
    prompt-history timestamps, and a rich summary.json imports end to end: the
    tool call, tool result, thinking, model names, and prompt time all land."""
    init_db()
    f = _write_grok(
        archive_home,
        [
            {"type": "user", "content": [{"type": "text",
             "text": "<user_info>ella</user_info><user_query>read a file</user_query>"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "I should read it"}]},
            {"type": "assistant", "content": "reading", "model_id": "grok-4-custom",
             "tool_calls": [{"id": "call1", "name": "read_file", "arguments": "{\"path\": \"/x\"}"}]},
            {"type": "tool_result", "tool_call_id": "call1", "content": "file contents"},
            {"type": "assistant", "content": "done"},
        ],
        summary={
            "current_model_id": "grok-4", "generated_title": "My Grok Session",
            "info": {"id": "sess-123", "cwd": "/proj"}, "created_at": "2026-01-01T10:00:00Z",
            "git_root_dir": "/proj", "head_branch": "main",
        },
        updates=[
            {"timestamp": 1735725600, "params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "call1"}}},
            {"timestamp": 1735725601, "params": {"update": {
                "sessionUpdate": "tool_call_update", "toolCallId": "call1", "status": "completed"}}},
            {"timestamp": 1735725602, "params": {"update": {
                "sessionUpdate": "tool_call_update", "toolCallId": "call_orphan"}}},  # no status → start
            {"timestamp": 1735725603, "params": {"update": {"sessionUpdate": "other_kind", "toolCallId": "z"}}},
            {"timestamp": 1735725604, "params": {"update": {"sessionUpdate": "tool_call"}}},  # no toolCallId
            "",            # blank line skipped
            "{not json",   # bad line skipped
        ],
        prompts=[
            {"session_id": "grok-sess", "timestamp": "2026-01-01T10:00:00Z", "prompt": "read a file"},
            {"session_id": "other-sess", "timestamp": "2026-01-01T10:00:00Z", "prompt": "ignore me"},
            {"session_id": "grok-sess", "prompt": "no timestamp"},  # no ts → skipped
            "",
            "{bad",
        ],
    )

    r = import_grok_session_incremental(f, "grok-sess")
    assert r.is_new_thread and r.events_created > 0
    assert _threads()["grok-sess"].title == "My Grok Session"

    events = _events()
    # Tool call + result landed with the resolved name.
    use = next(p for t, p in events if t == "tool_use_complete")
    assert use["tool_name"] == "read_file" and use["tool_call_id"] == "call1"
    assert use["input"] == {"path": "/x"}
    res = next(p for t, p in events if t == "tool_execution_completed")
    assert res["tool_call_id"] == "call1" and "file contents" in json.dumps(res["output"])
    # Reasoning survived as a thinking event.
    assert any(t == "thinking_complete" and "I should read it" in (p.get("text") or "")
               for t, p in events)
    # Model comes from the assistant line (model_id) for turn 1, session default for turn 2.
    models = [p["model"] for t, p in events if t == "api_request_started"]
    assert models == ["grok-4-custom", "grok-4"]
    # The user prompt time was matched from prompt_history.jsonl (not inferred).
    user_ev = next(p for t, p in events if t == "user_message_sent")
    assert user_ev.get("timestamp_inferred") is not True

    n = len(events)
    r2 = import_grok_session_incremental(f, "grok-sess")
    assert r2.events_created == 0 and len(_events()) == n


def test_grok_no_importable_content_is_skipped_not_threaded(archive_home) -> None:
    """A session whose new lines carry nothing importable (a lone system line)
    creates no thread and imports no events — the has-importable gate returns
    False and the watermark advances on the skip ledger."""
    init_db()
    f = _write_grok(archive_home, [
        {"type": "system", "content": "just a system prompt, no turns"},
    ])
    r = import_grok_session_incremental(f, "grok-empty")
    assert r.events_created == 0 and r.thread_id == 0 and not r.is_new_thread
    assert _threads() == {}


def test_grok_non_dict_line_and_empty_turns_are_skipped() -> None:
    """_build_grok_messages: a non-dict line, an empty-text user turn, and an
    empty assistant turn (no content/tool/thinking) are all skipped; a tool_result
    with no open assistant opens a fresh one, and dangling reasoning folds into the
    next assistant."""
    msgs = _build_grok_messages(
        [
            "a bare string line",                                       # non-dict → skipped
            {"type": "assistant", "content": "", "tool_calls": []},     # empty → built None
            {"type": "user", "content": []},                            # no text → user None
            {"type": "tool_result", "tool_call_id": "c1", "content": "out"},  # cur None → new assistant
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "th"}]},
            {"type": "assistant", "content": "final"},
        ],
        {}, {}, {}, "src",
    )
    assert len(msgs) == 2
    # First message is the assistant opened by the orphan tool_result.
    assert msgs[0]["role"] == "assistant"
    assert any(b["type"] == "tool_result" for b in msgs[0]["content_blocks"])
    # Second carries the folded-in thinking plus the final text.
    kinds = [b["type"] for b in msgs[1]["content_blocks"]]
    assert "thinking" in kinds and "text" in kinds


def test_grok_user_text_variants() -> None:
    assert _grok_user_text({"content": "plain"}) == "plain"
    assert _grok_user_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "ab"
    assert _grok_user_text({"content": [{"type": "image"}]}) is None  # list, no text parts
    assert _grok_user_text({"content": 42}) is None                   # neither str nor list


def test_grok_extract_query_variants() -> None:
    assert _grok_extract_query("<user_query> hi </user_query>") == "hi"
    assert _grok_extract_query("<system-reminder>stay terse</system-reminder>") == ""
    assert _grok_extract_query("just a plain prompt") == "just a plain prompt"


def test_grok_reasoning_text_variants() -> None:
    text = _grok_reasoning_text({"summary": [
        {"type": "summary_text", "text": "one"}, {"type": "other"}, {"type": "summary_text", "text": "two"}]})
    assert text == "one\n\ntwo"
    assert _grok_reasoning_text({"summary": "not a list"}) == ""


def test_grok_tool_input_variants() -> None:
    assert _grok_tool_input({"arguments": {"a": 1}}) == {"a": 1}          # dict
    assert _grok_tool_input({"arguments": "{\"a\": 1}"}) == {"a": 1}      # valid json str
    assert _grok_tool_input({"arguments": "not json"}) == {"arguments": "not json"}  # bad json
    assert _grok_tool_input({"arguments": "[1, 2]"}) == {"arguments": [1, 2]}         # json non-dict
    assert _grok_tool_input({}) == {}                                    # nothing


def test_grok_model_and_accumulate_thinking() -> None:
    assert _grok_model({"current_model_id": "  grok-4  "}) == "grok-4"
    assert _grok_model({"current_model_id": ""}) == "grok"
    assert _grok_model({}) == "grok"
    assert _grok_accumulate_thinking("prior", {"summary": []}) == "prior"   # no new thought
    assert _grok_accumulate_thinking(None, {"summary": [{"type": "summary_text", "text": "x"}]}) == "x"
    assert _grok_accumulate_thinking("a", {"summary": [{"type": "summary_text", "text": "b"}]}) == "a\n\nb"


def test_grok_harvest_tool_names() -> None:
    names = _harvest_tool_names([
        "not a dict",
        {"type": "user"},
        {"type": "assistant", "tool_calls": "not a list"},
        {"type": "assistant", "tool_calls": [{"id": "c1", "name": "grep"}, {"id": "", "name": "skip"},
                                             {"id": "c2"}]},
    ])
    assert names == {"c1": "grep", "c2": "unknown"}


def test_grok_pop_prompt_ts_exact_prefix_and_miss() -> None:
    from datetime import datetime, timezone

    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    times = {"continue": [t1], "hello": [t2]}
    # Exact match pops the queue.
    assert _grok_pop_prompt_ts(times, "hello") == t2
    # A longer query prefix-matches the stored short prompt.
    assert _grok_pop_prompt_ts(times, "continue with the rest of it") == t1
    # Nothing left / no match.
    assert _grok_pop_prompt_ts(times, "totally unrelated") is None


def test_grok_tool_result_block_variants() -> None:
    assert _grok_tool_result_block({"tool_call_id": ""}, {}, {}) is None   # invalid cid
    block = _grok_tool_result_block(
        {"tool_call_id": "c1", "content": {"nested": True}},
        {"c1": {"status": "failed", "end": None}}, {"c1": "grep"})
    assert block["name"] == "grep" and block["is_error"] is True
    assert json.loads(block["content"]) == {"nested": True}               # non-str output → json


def test_grok_build_assistant_message_none_and_populated() -> None:
    # Empty everything → None.
    assert _grok_build_assistant_message(
        {"content": "", "tool_calls": []}, None, {}, {}, "grok", {}, "p") is None
    # Non-str content coerced; non-list tool_calls reset; tool block minted with start time.
    from datetime import datetime, timezone

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    msg = _grok_build_assistant_message(
        {"content": 123, "tool_calls": [{"id": "c1", "name": "t", "arguments": {}}]},
        "thinking here", {"c1": {"start": start}}, {}, "grok", {}, "p")
    kinds = [b["type"] for b in msg["content_blocks"]]
    assert kinds == ["thinking", "text", "tool_use"]
    assert msg["content_blocks"][2]["start_timestamp"] == start.isoformat()

    # Non-list tool_calls reset to []; empty content but pending thinking still builds;
    # a second tool_call with no time and an id-less tool_call are handled.
    msg2 = _grok_build_assistant_message(
        {"content": "  ", "tool_calls": "not a list"}, "just thinking", {}, {}, "grok", {}, "p")
    assert [b["type"] for b in msg2["content_blocks"]] == ["thinking"]
    msg3 = _grok_build_assistant_message(
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "t", "arguments": {}},   # has start time
            {"id": "c2", "name": "t2", "arguments": {}},  # no start time → not earlier
            {"name": "noid"},                              # id-less → skipped
        ]},
        "pending", {"c1": {"start": start}}, {}, "grok", {}, "p")
    tool_ids = [b["id"] for b in msg3["content_blocks"] if b["type"] == "tool_use"]
    assert tool_ids == ["c1", "c2"]  # id-less skipped
    assert not any(b["type"] == "text" for b in msg3["content_blocks"])  # empty content skipped


def test_grok_fold_tool_update_branches() -> None:
    from datetime import datetime, timezone

    out: dict = {}
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # tool_call sets start.
    _grok_fold_tool_update(
        {"timestamp": ts.timestamp(), "params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "c1"}}}, out)
    assert "start" in out["c1"]
    # tool_call_update with status sets status + end.
    _grok_fold_tool_update(
        {"timestamp": ts.timestamp(), "params": {"update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}}}, out)
    assert out["c1"]["status"] == "completed" and "end" in out["c1"]
    # Unmodeled sessionUpdate → ignored.
    _grok_fold_tool_update({"params": {"update": {"sessionUpdate": "nope", "toolCallId": "c1"}}}, out)
    # Missing toolCallId → ignored.
    _grok_fold_tool_update({"params": {"update": {"sessionUpdate": "tool_call"}}}, out)
    # A repeat tool_call for c1 (start already present, no ts effect) is a no-op.
    _grok_fold_tool_update({"params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "c1"}}}, out)
    assert set(out) == {"c1"}

    # A fresh id whose only update carries a status but no timestamp → status, no end.
    _grok_fold_tool_update(
        {"params": {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"}}}, out)
    assert out["c2"] == {"status": "failed"}
    # A fresh id whose update has neither status nor timestamp → left empty.
    _grok_fold_tool_update(
        {"params": {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "c3"}}}, out)
    assert out["c3"] == {}


def test_grok_tool_result_fold_ignores_invalid_result() -> None:
    """A tool_result with no call id builds no block, so the open assistant turn is
    returned unchanged (nothing spurious appended)."""
    msgs = _build_grok_messages(
        [
            {"type": "assistant", "content": "hi"},
            {"type": "tool_result", "tool_call_id": ""},  # invalid → folded to nothing
        ],
        {}, {}, {}, "src",
    )
    assert len(msgs) == 1
    assert [b["type"] for b in msgs[0]["content_blocks"]] == ["text"]


def test_grok_sibling_files_that_are_directories_are_tolerated(archive_home) -> None:
    """updates.jsonl / prompt_history.jsonl existing as directories (an OSError on
    open) degrade to empty maps rather than crashing the import."""
    session_dir = archive_home / "sess"
    session_dir.mkdir()
    (session_dir / "updates.jsonl").mkdir()
    (archive_home / "prompt_history.jsonl").mkdir()
    assert _grok_tool_timestamps(session_dir) == {}
    assert _grok_prompt_ts_map(session_dir, "sess") == {}


def test_grok_session_meta_variants(archive_home) -> None:
    d = archive_home / "s"
    d.mkdir()
    assert _grok_session_meta(d) == {}                       # no file
    (d / "summary.json").write_text("{not valid", encoding="utf-8")
    assert _grok_session_meta(d) == {}                       # bad json
    (d / "summary.json").write_text("[1, 2]", encoding="utf-8")
    assert _grok_session_meta(d) == {}                       # non-dict
    (d / "summary.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")
    assert _grok_session_meta(d) == {"k": "v"}


def test_grok_has_importable_content_branches() -> None:
    assert _grok_has_importable_content([{"type": "assistant", "content": "hi"}]) is True
    assert _grok_has_importable_content([{"type": "assistant", "content": "", "tool_calls": [{"id": "c"}]}]) is True
    assert _grok_has_importable_content([{"type": "tool_result", "tool_call_id": "c"}]) is True
    assert _grok_has_importable_content(
        [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "t"}]}]) is True
    assert _grok_has_importable_content(
        [{"type": "user", "content": [{"type": "text", "text": "<user_query>q</user_query>"}]}]) is True
    # An empty assistant, empty reasoning, synthetic user, context-only user, and a
    # system line all fail to signal content (each falls through to the next line).
    assert _grok_has_importable_content([
        {"type": "assistant", "content": "  ", "tool_calls": []},
        {"type": "reasoning", "summary": []},
        {"type": "user", "synthetic_reason": "x", "content": [{"type": "text", "text": "hi"}]},
        {"type": "user", "content": [{"type": "text", "text": "<user_info>ctx</user_info>"}]},
        {"type": "system", "content": "sys"},
    ]) is False


def test_grok_title_variants() -> None:
    assert _grok_title([], {"generated_title": "  Gen Title  "}) == "Gen Title"
    assert _grok_title([], {"session_summary": "Summary Title"}) == "Summary Title"
    long_q = "x" * 150
    lines = [{"type": "user", "content": [{"type": "text", "text": f"<user_query>{long_q}</user_query>"}]}]
    title = _grok_title(lines, {})
    assert title.endswith("...") and len(title) == 100
    assert _grok_title([{"type": "system", "content": "s"}], {}) == "Grok Session"


def test_grok_first_query_skips_and_returns_none() -> None:
    # A synthetic user, an assistant, a text-less user, and a context-only user are
    # all skipped → no query found.
    lines = [
        {"type": "assistant", "content": "hi"},
        {"type": "user", "synthetic_reason": "x", "content": [{"type": "text", "text": "syn"}]},
        {"type": "user", "content": 42},                                  # text None
        {"type": "user", "content": [{"type": "text", "text": "<user_info>c</user_info>"}]},  # empty query
    ]
    assert _grok_first_query(lines) is None
    lines.append({"type": "user", "content": [{"type": "text", "text": "<user_query>real</user_query>"}]})
    assert _grok_first_query(lines) == "real"


# ══════════════════════════════════════════════════════════════════════════
# CODEX
# ══════════════════════════════════════════════════════════════════════════


CODEX_RICH = [
    {"type": "session_meta", "payload": {"id": "s", "cwd": "/proj", "thread_name": "Named Session"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"type": "user_message", "message": "do a thing", "turn_id": "t1"}},
    {"type": "response_item", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "planning"}],
                 "content": ["and more"]}},
    {"type": "response_item", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "mytool", "input": {"q": 1}}},
    {"type": "response_item", "timestamp": "2026-01-01T10:00:03Z",
     "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "tool result text"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:04Z",
     "payload": {"type": "agent_message", "message": "finished"}},
]


def test_codex_full_session_reasoning_and_custom_tool(archive_home) -> None:
    """A codex session with reasoning, a custom_tool_call + its output, and a
    thread_name title imports end to end: thinking, the tool call (with its dict
    input), and the tool result all land."""
    init_db()
    f = archive_home / "codex.jsonl"
    _write_jsonl(f, CODEX_RICH)

    r = import_codex_session_incremental(f, "codex-rich")
    assert r.is_new_thread and r.events_created > 0
    assert _threads()["codex-rich"].title == "Named Session"

    events = _events()
    assert any(t == "thinking_complete" and "planning" in (p.get("text") or "") for t, p in events)
    use = next(p for t, p in events if t == "tool_use_complete")
    assert use["tool_name"] == "mytool" and use["input"] == {"q": 1}
    res = next(p for t, p in events if t == "tool_execution_completed")
    assert "tool result text" in json.dumps(res["output"])

    n = len(events)
    assert import_codex_session_incremental(f, "codex-rich").events_created == 0
    assert len(_events()) == n


def test_codex_line_model_variants() -> None:
    assert _codex_line_model({"type": "turn_context", "payload": "notdict"}) is None
    assert _codex_line_model({"type": "turn_context", "payload": {"model": "gpt-5"}}) == "gpt-5"
    assert _codex_line_model({"type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": {"model": "gpt-x"}}}) == "gpt-x"
    assert _codex_line_model({"type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": "oops"}}) is None
    assert _codex_line_model({"type": "session_meta", "payload": {"model": "old"}}) == "old"
    assert _codex_line_model({"type": "response_item", "payload": {"type": "reasoning"}}) is None  # other
    assert _codex_line_model({"type": "turn_context", "payload": {"model": "   "}}) is None       # blank


def test_codex_model_scan() -> None:
    # reversed scan hits the non-dict entry first (continue), then finds the model.
    assert _codex_model([{"type": "turn_context", "payload": {"model": "gpt-5"}}, "notdict"]) == "gpt-5"
    assert _codex_model([{"type": "event_msg", "payload": {}}]) == "codex"


def test_codex_has_importable_content_branches() -> None:
    assert _codex_has_importable_content([{"type": "event_msg", "payload": "notdict"}]) is False  # non-dict
    assert _codex_has_importable_content([{"type": "event_msg", "payload": {
        "type": "user_message", "message": ""}}]) is False                                       # empty msg
    assert _codex_has_importable_content([{"type": "event_msg", "payload": {
        "type": "agent_message", "message": "hi"}}]) is True


def test_codex_first_user_line_and_title() -> None:
    lines = [
        {"type": "event_msg", "payload": "notdict"},                                      # non-dict skip
        {"type": "event_msg", "payload": {"type": "user_message", "message": "   "}},      # empty skip
        {"type": "event_msg", "payload": {"type": "user_message", "message": "\n\nreal line\nmore"}},
    ]
    assert _codex_first_user_line(lines) == "real line"
    assert _codex_first_user_line([{"type": "event_msg", "payload": {"type": "agent_message"}}]) is None
    # Title falls back to the first user line, then to the constant.
    assert _codex_title(lines) == "real line"
    assert _codex_title([{"type": "session_meta", "payload": {"thread_name": "  Named  "}}]) == "Named"
    assert _codex_title([{"type": "response_item", "payload": {"type": "reasoning"}}]) == "Codex Session"


def test_codex_call_input_variants() -> None:
    assert _codex_call_input({"input": {"a": 1}}, "custom_tool_call") == {"a": 1}
    assert _codex_call_input({"input": 5}, "custom_tool_call") == {"input": "5"}
    assert _codex_call_input({}, "custom_tool_call") is None
    assert _codex_call_input({"arguments": "{\"a\": 1}"}, "function_call") == {"a": 1}
    assert _codex_call_input({"arguments": "bad json"}, "function_call") == {"arguments": "bad json"}
    assert _codex_call_input({"arguments": "[1]"}, "function_call") is None   # json non-dict
    assert _codex_call_input({"arguments": {"a": 2}}, "function_call") == {"a": 2}
    assert _codex_call_input({}, "function_call") is None


def test_codex_call_maps_branches() -> None:
    names, inputs = _codex_call_maps([
        {"type": "response_item", "payload": "notdict"},                                    # non-dict payload
        {"type": "event_msg", "payload": {"type": "user_message"}},                         # not response_item
        {"type": "response_item", "payload": {"type": "reasoning"}},                        # non-tool type
        {"type": "response_item", "payload": {"type": "function_call", "name": "x"}},       # no call_id
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1", "name": "shell",
                                              "arguments": "{}"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c2"}},   # no name, no input
    ])
    assert names == {"c1": "shell"}
    assert set(inputs) == {"c1"}


def test_codex_reasoning_text() -> None:
    payload = {"summary": [{"type": "s", "text": "a"}, "b", {"no": "text"}], "content": "c"}
    assert _codex_reasoning_text(payload) == "a\nb\nc"
    assert _codex_reasoning_text({"summary": "plain summary"}) == "plain summary"
    assert _codex_reasoning_text({}) == ""


def test_codex_user_message_and_tool_use_block() -> None:
    assert _codex_user_message({"message": ""}, "ts") is None
    msg = _codex_user_message({"message": "hi", "turn_id": "t1"}, "ts")
    assert msg["role"] == "user" and msg["provider_message_id"] == "t1"
    assert _codex_tool_use_block({"call_id": ""}, "ts", {}, {}) is None
    block = _codex_tool_use_block({"call_id": "c1", "name": "raw"}, "ts", {}, {"c1": {"a": 1}})
    assert block["name"] == "raw" and block["input"] == {"a": 1}


def test_codex_output_is_error_variants() -> None:
    assert _codex_output_is_error("Process exited with code 3\nOutput:\nboom") is True
    assert _codex_output_is_error("Exit code: 0\nfine") is False
    assert _codex_output_is_error("{not valid json") is False           # json decode fail
    assert _codex_output_is_error('{"exit_code": 2}') is True
    assert _codex_output_is_error('{"metadata": {"exit_code": 4}}') is True   # nested
    assert _codex_output_is_error('{"exit_code": "nope"}') is False     # non-int
    assert _codex_output_is_error('{"exit_code": true}') is False       # bool excluded
    assert _codex_output_is_error("no signal at all") is False


def test_codex_tool_result_block_variants() -> None:
    assert _codex_tool_result_block({"call_id": ""}, "ts", {}) is None
    block = _codex_tool_result_block({"call_id": "c1", "output": {"nested": 1}}, "ts", {"c1": "grep"})
    assert block["name"] == "grep"
    assert json.loads(block["content"]) == {"nested": 1}               # non-str output → json


def test_codex_assistant_block_reasoning_and_preserve() -> None:
    block = _codex_assistant_block(
        "response_item", "reasoning", {"summary": [{"type": "s", "text": "think"}]}, "ts", {}, {})
    assert block["type"] == "thinking" and block["text"] == "think"
    # Empty reasoning → None (nothing to keep in a thinking block).
    assert _codex_assistant_block("response_item", "reasoning", {}, "ts", {}, {}) is None
    # session_meta lines are consumed upstream → None.
    assert _codex_assistant_block("session_meta", None, {"cwd": "/x"}, "ts", {}, {}) is None


def test_codex_build_messages_skips_bad_lines() -> None:
    msgs = _build_codex_messages([
        "not a dict",                                           # non-dict line
        {"type": "event_msg", "payload": "notdict"},            # non-dict payload
        {"type": "event_msg", "payload": {"type": "user_message"}},  # empty → user None
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
    ], "codex", {}, {})
    assert len(msgs) == 1 and msgs[0]["role"] == "assistant"
    assert msgs[0]["content_blocks"][0]["text"] == "hi"


# ══════════════════════════════════════════════════════════════════════════
# CURSOR
# ══════════════════════════════════════════════════════════════════════════


def _make_cursor_db(path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_cursor_no_table_returns_empty(archive_home) -> None:
    """A state.vscdb without the cursorDiskKV table scans to an empty result."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE somethingElse (k TEXT)")
    conn.commit()
    conn.close()
    scan = import_cursor_db(db)
    assert scan.processed == 0 and scan.imported == 0 and scan.events_created == 0


def test_cursor_malformed_bubble_key_skipped(archive_home) -> None:
    """A bubbleId key with fewer than 3 colon-parts is logged and skipped without
    derailing the composer it should have belonged to."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_mk"
    composer = {"name": "MK", "lastUpdatedAt": 9999999999999,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    _make_cursor_db(db, [
        (f"composerData:{cid}", json.dumps(composer)),
        ("bubbleId:onlyonepart", json.dumps({"type": 1, "text": "orphan"})),  # malformed key
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])
    scan = import_cursor_db(db)
    assert scan.processed == 1
    assert any(t == "user_message_sent" for t, _ in _events())


def test_cursor_import_from_payload_with_session(archive_home) -> None:
    """import_cursor_from_payload runs inside a passed session (no own transaction)."""
    init_db()
    composer = {"name": "Direct", "lastUpdatedAt": 9999999999999,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    bubbles = {"cD:b1": {"type": 1, "text": "hello direct", "createdAt": 1700000000000}}
    with get_session() as s:
        result = import_cursor_from_payload(
            composer_id="cD", composer_data=composer, bubbles=bubbles, session=s)
        s.commit()
    assert result.events_created > 0 and result.is_new_thread


def test_cursor_empty_headers_imports_nothing(archive_home) -> None:
    """A composer with no conversation headers builds no messages → no events."""
    init_db()
    with get_session() as s:
        result = import_cursor_from_payload(
            composer_id="cE", composer_data={"name": "Empty", "fullConversationHeadersOnly": []},
            bubbles={}, session=s)
        s.commit()
    assert result.events_created == 0 and result.thread_id == 0


def test_cursor_reimport_no_new_messages_resolves_existing_thread(archive_home) -> None:
    """A changed composer (future lastUpdatedAt) whose message set is fully behind
    the line watermark resolves to the existing thread and imports nothing new."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_re"
    composer = {"name": "Re", "lastUpdatedAt": 9999999999999,   # far future → never "unchanged"
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    _make_cursor_db(db, [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])
    first = import_cursor_db(db)
    assert first.imported == 1
    n = len(_events())
    second = import_cursor_db(db)   # same data, still not "unchanged", but no new messages
    assert second.imported == 0 and second.events_created == 0
    assert len(_events()) == n


def test_cursor_long_composer_name_truncated_on_new_thread(archive_home) -> None:
    """A new thread's title is truncated to 100 chars from the composer name."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_long"
    composer = {"name": "N" * 150, "lastUpdatedAt": 9999999999999,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    _make_cursor_db(db, [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])
    import_cursor_db(db)
    title = _threads()[cid].title
    assert len(title) == 100 and title.endswith("...")


def test_cursor_long_name_stub_title_truncated(archive_home, monkeypatch) -> None:
    """A blown-up composer with a very long name gets a truncated stub-thread title."""
    init_db()
    db = archive_home / "state.vscdb"
    cid = "comp_boomlong"
    composer = {"name": "L" * 150, "lastUpdatedAt": 9999999999999,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    _make_cursor_db(db, [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cursor_mod, "import_cursor_from_payload", _boom)
    import_cursor_db(db)
    stub = _threads()[f"{cid}:import-error"]
    assert len(stub.title) == 100 and stub.title.endswith("...")


def test_cursor_stub_failure_is_swallowed(archive_home, monkeypatch) -> None:
    """When even the error-stub preservation raises, import_cursor_db must not
    crash — both the corrupt-load path and the blow-up path swallow the inner
    failure so the whole scan still completes."""
    init_db()
    db = archive_home / "state.vscdb"
    good = {"name": "Good", "lastUpdatedAt": 9999999999999,
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]}
    _make_cursor_db(db, [
        ("composerData:corrupt", "{not json at all"),          # corrupt-load path
        ("composerData:good", json.dumps(good)),               # parses, but import boomed below
        ("bubbleId:good:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])

    def _boom_stub(*a, **k):
        raise RuntimeError("stub failed too")

    def _boom_import(*a, **k):
        raise RuntimeError("import failed")

    monkeypatch.setattr(cursor_mod, "_import_cursor_error_stub", _boom_stub)
    monkeypatch.setattr(cursor_mod, "import_cursor_from_payload", _boom_import)
    scan = import_cursor_db(db)   # must not raise
    assert scan.failed >= 1


def test_cursor_build_messages_edge_shapes() -> None:
    """_build_cursor_messages: non-dict headers, missing bubbleId, an unknown-type
    header with a missing bubble (no raw), empty/absent thinking, and the rawArgs
    fallback in three shapes are all handled."""
    assert _build_cursor_messages("c", {"fullConversationHeadersOnly": []}, {}) == []  # no headers
    composer = {"fullConversationHeadersOnly": [
        "not a dict",                      # skipped
        {"noBubbleId": True},              # skipped
        {"bubbleId": "u1", "type": 1},     # user, thinking non-dict
        {"bubbleId": "unk", "type": 5},    # unknown type, bubble MISSING (empty) → no raw
        {"bubbleId": "aValid", "type": 2}, # rawArgs valid json
        {"bubbleId": "aBad", "type": 2},   # rawArgs invalid json
        {"bubbleId": "aNum", "type": 2},   # rawArgs non-str
    ]}
    bubbles = {
        "c:u1": {"type": 1, "text": "hi", "thinking": "not a dict"},
        "c:aValid": {"type": 2, "toolFormerData": {"name": "t", "toolCallId": "c1", "rawArgs": "{\"x\": 1}"}},
        "c:aBad": {"type": 2, "toolFormerData": {"name": "t", "toolCallId": "c2", "rawArgs": "not json"}},
        "c:aNum": {"type": 2, "toolFormerData": {"name": "t", "toolCallId": "c3", "rawArgs": 42}},
    }
    msgs = _build_cursor_messages("c", composer, bubbles)
    by_id = {m["id"]: m for m in msgs}
    assert set(by_id) == {"u1", "unk", "aValid", "aBad", "aNum"}
    assert by_id["unk"]["role"] == "unknown" and "raw" not in by_id["unk"]
    assert by_id["aValid"]["tool_call"]["input"] == {"x": 1}
    assert by_id["aBad"]["tool_call"]["input"] == "not json"
    assert by_id["aNum"]["tool_call"]["input"] == 42


def test_cursor_build_messages_empty_thinking_text() -> None:
    """A thinking dict with empty text produces no `thinking` key on the message."""
    composer = {"fullConversationHeadersOnly": [{"bubbleId": "a1", "type": 2}]}
    bubbles = {"c:a1": {"type": 2, "text": "hi", "thinking": {"text": ""}}}
    msg = _build_cursor_messages("c", composer, bubbles)[0]
    assert "thinking" not in msg


def test_cursor_tool_blocks_result_shapes() -> None:
    # No result → only the tool_use block.
    only_use = _cursor_tool_blocks({"call_id": "c1", "name": "t", "input": {"a": 1}})
    assert len(only_use) == 1 and only_use[0]["type"] == "tool_use"
    # String result → passthrough content; dict result → json-encoded; error status.
    str_res = _cursor_tool_blocks({"call_id": "c2", "name": "t", "result": "plain", "status": "error"})
    assert str_res[1]["content"] == "plain" and str_res[1]["is_error"] is True
    dict_res = _cursor_tool_blocks({"call_id": "c3", "name": "t", "result": {"k": "v"}})
    assert json.loads(dict_res[1]["content"]) == {"k": "v"} and dict_res[1]["is_error"] is False


def test_cursor_to_normalized_assistant_and_unknown() -> None:
    # Assistant with thinking but no content → thinking block only, no text block.
    out = _cursor_to_normalized({"role": "assistant", "id": "a1", "thinking": "hmm", "content": ""})
    kinds = [b["type"] for b in out["content_blocks"]]
    assert kinds == ["thinking"]
    # Unknown role with no raw → content carried, no raw in provider_data.
    unk = _cursor_to_normalized({"role": "", "id": "x", "content": "keep me"})
    assert unk["role"] == "unknown" and unk["content_text"] == "keep me"
    assert "raw" not in unk["provider_data"]
