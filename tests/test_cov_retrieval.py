"""Branch-coverage tests for the lexical/read retrieval paths.

Covers the uncovered edges of the non-vector retrieval cluster:

  * ``read`` — redacted payloads, unknown-event surfacing, live-capture stream
    absorption (content_blocks / stitched deltas / arc-less orphans), result-block
    gluing, tool/result truncation, the summary TOC edges, focused-read budget
    retry, and the structured (web-viewer) renderer's toggles and marker types.
  * ``_extract`` — every extractor's content/empty branches, block-text scrubbing,
    frontmatter stripping, and the mcp/heredoc tool-use path.
  * ``_codex`` — each preserved-block renderer, the machinery/echo hides, and the
    unknown-kind JSON dump (truncation + non-serialisable fallback).
  * ``fts`` — hit-dict timestamp normalisation, filter clauses, the twin gate and
    delta stitching, meta-doc anchoring edges, orphan-call rebuild, and status.

Events are seeded directly (no importer) so the rendering/indexing contract is
tested independently of any provider parser.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from thread_archive._retrieval import search
from thread_archive._retrieval._codex import (
    codex_kind,
    render_codex_block,
)
from thread_archive._retrieval._extract import (
    _block_search_text,
    _fts_tool_completed,
    _fts_tool_use,
    _strip_frontmatter,
    _to_str,
    extract_fts_content,
)
from thread_archive._retrieval.fts import (
    _cached_twin_texts,
    _gate_arc_tuples,
    build_event_hit,
    fts_status,
    index_thread_meta,
    rebuild_fts,
    search_events,
    stitch_delta_tuples,
)
from thread_archive._retrieval.read import (
    _fmt_hm,
    _fmt_ts,
    _unknown_payload_text,
    read_thread,
    read_thread_structured,
)
from thread_archive._store import Event, Thread, get_session, init_db, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


def _seed(events, *, tid=1, thread_type="conversation", title="T",
          source="claude-code", source_id=None):
    """events: (event_type, payload, minute) or (event_type, payload, minute, api_call_id)."""
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title=title, thread_type=thread_type,
                     source=source, source_id=source_id,
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for ev in events:
            et, payload, minute = ev[0], ev[1], ev[2]
            api_call_id = ev[3] if len(ev) > 3 else None
            # Let SQLite assign the global event id (autoincrement) so several
            # _seed calls in one test never collide on events.id.
            s.add(Event(thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute), api_call_id=api_call_id))
        s.commit()
    return tid


# ══════════════════════════════════════════════════════════════════════════════
# read.py — small pure helpers
# ══════════════════════════════════════════════════════════════════════════════

def test_fmt_ts_and_hm_accept_string_timestamps() -> None:
    # Non-datetime (a stored string) path of each formatter.
    assert _fmt_ts("2026-01-01 10:20:30.123456") == "2026-01-01T10:20:30"
    assert _fmt_hm("2026-01-01 10:20:30.123456") == "10:20"
    # A too-short string falls through the slice guard rather than raising.
    assert _fmt_hm("2026") == "2026"


def test_unknown_payload_text_json_dump_and_circular() -> None:
    # No obvious text key → compact JSON dump, provider_data omitted, capped at 1000.
    out = _unknown_payload_text({"foo": "bar", "provider_data": "x" * 5000})
    assert "bar" in out and "provider_data" not in out and len(out) <= 1000
    # A non-serialisable (circular) payload can't raise out of the renderer.
    a: dict = {}
    a["self"] = a
    assert _unknown_payload_text(a) == ""
    # An obvious text key is preferred verbatim.
    assert _unknown_payload_text({"error": "boom happened"}) == "boom happened"


# ══════════════════════════════════════════════════════════════════════════════
# read.py — string transcript rendering branches
# ══════════════════════════════════════════════════════════════════════════════

def test_redacted_payload_renders_placeholder(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "the question"}, 1),
        ("text_complete", {"_redacted": True}, 2),
    ])
    out = read_thread(tid, mode="chat")
    assert "[redacted]" in out


def test_context_summary_and_model_change_render_in_full(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("context_summary", {"content": "summarised earlier context"}, 2),
        ("model_change", {"to": "claude-opus-4-8"}, 3),
        ("text_complete", {"text": "done"}, 4),
    ])
    out = read_thread(tid, mode="full")
    assert "[context summary] summarised earlier context" in out
    assert "[model → claude-opus-4-8]" in out
    # A model_change with no target renders nothing (no [unknown] JSON dump).
    tid2 = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("model_change", {}, 2),
        ("text_complete", {"text": "x"}, 3),
    ], tid=2)
    assert "model →" not in read_thread(tid2, mode="full")


def test_tool_block_empty_input_and_long_value(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("tool_use_complete", {"tool_name": "Ping", "input": {}}, 2),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "x" * 200}}, 3),
        ("text_complete", {"text": "done"}, 4),
    ])
    out = read_thread(tid, mode="full")
    assert "[tool: Ping]" in out                 # empty input → bare form
    assert "command=" + ("x" * 80) + "..." in out  # long value truncated at 80


def test_tool_error_truncated_with_results(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "c"}}, 2),
        ("tool_execution_error", {"error": "E" * 5000}, 2),
        ("text_complete", {"text": "failed"}, 3),
    ])
    out = read_thread(tid, mode="full", tool_results=True)
    assert "[tool error]" in out and "… (truncated)" in out
    assert out.count("E") <= 2100


def test_result_glued_onto_closed_assistant_step(archive_home) -> None:
    # The result event lands AFTER the text that closed its step → it must glue back
    # onto that assistant step, not spawn an orphan result-only step.
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 2),
        ("text_complete", {"text": "step closes here"}, 3),
        ("tool_execution_completed", {"output": "LATERESULT"}, 4),
    ])
    out = read_thread(tid, mode="full", tool_results=True)
    assert "[result] LATERESULT" in out
    assert out.count("[ASSISTANT") == 1  # single assistant step, not two


def test_empty_user_message_is_skipped(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "   "}, 1),   # whitespace-only → skipped
        ("user_message_sent", {"content": "real question"}, 2),
        ("text_complete", {"text": "reply"}, 3),
    ])
    out = read_thread(tid, mode="user")
    assert "real question" in out
    assert "1 user turns" in out.splitlines()[2]  # the empty one did not become a turn


def test_message_role_turn_empty_content_skipped(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("message", {"role": "tool", "content": ""}, 2),   # empty → dropped
        ("message", {"role": "tool", "content": "plugin says hi"}, 3),
        ("text_complete", {"text": "ok"}, 4),
    ])
    out = read_thread(tid, mode="full")
    assert "plugin says hi" in out and "[TOOL" in out


def test_assistant_only_leading_turn_in_user_mode(archive_home) -> None:
    # A thread that opens with assistant text (no user step) → in user_only mode the
    # leading turn formats to nothing, exercising the empty-turn accumulate branch.
    tid = _seed([
        ("text_complete", {"text": "assistant speaks first"}, 1),
        ("user_message_sent", {"content": "then the user"}, 2),
        ("text_complete", {"text": "and a reply"}, 3),
    ])
    out = read_thread(tid, mode="user")
    assert "then the user" in out
    assert "assistant speaks first" not in out


# ── live-capture stream absorption ────────────────────────────────────────────

_LIVE = [
    # inference 1: summary carries assembled content_blocks, no granular twins
    ("user_message_sent", {"content": "live q one"}, 1),
    ("api_request_completed", {"content_blocks": [
        {"type": "thinking", "thinking": "cb thinking here"},
        {"type": "text", "text": "cb reply here"}]}, 2, "c1"),
    # inference 2: summary with NO content_blocks → text stitched from raw deltas
    ("user_message_sent", {"content": "live q two"}, 3),
    ("text_delta", {"text": "stitched ", "block_index": 0}, 4, "c2"),
    ("text_delta", {"text": "reply", "block_index": 0}, 4, "c2"),
    ("api_request_completed", {}, 5, "c2"),
    # inference 3: deltas with no summary at all (killed mid-stream) → orphan stitch
    ("user_message_sent", {"content": "live q three"}, 6),
    ("thinking_delta", {"text": "orphan thought", "block_index": 0}, 7, "c3"),
]


def test_absorb_stream_deltas_synthesises_twins(archive_home) -> None:
    tid = _seed(_LIVE)
    full = read_thread(tid, mode="full")
    assert "cb reply here" in full                    # content_blocks path
    assert "cb thinking here" in full
    assert "stitched reply" in full                   # delta-stitch path
    assert "orphan thought" in full                   # arc-less orphan path
    chat = read_thread(tid, mode="chat")
    assert "cb reply here" in chat and "stitched reply" in chat
    assert "cb thinking here" not in chat             # thinking stripped in chat


def test_absorb_skips_delta_already_carried_by_a_real_twin(archive_home) -> None:
    # A doubly-captured turn: a real text_complete twin under one api_call, plus a
    # summary under a DIFFERENT api_call whose content_blocks repeat that text. The
    # synthesiser must not render the duplicate a second time.
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("text_complete", {"text": "the shared reply"}, 2, "twin"),
        ("api_request_completed", {"content_blocks": [
            {"type": "text", "text": "the shared reply"}]}, 3, "stream"),
    ])
    full = read_thread(tid, mode="full")
    assert full.count("the shared reply") == 1


# ── summary TOC edges ─────────────────────────────────────────────────────────

def test_summary_toc_empty_conversation(archive_home) -> None:
    tid = _seed([], tid=3)
    assert "has no messages yet" in read_thread(tid, summary=True)


def test_summary_toc_offset_edges(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "one"}, 1),
        ("text_complete", {"text": "reply one"}, 2),
        ("user_message_sent", {"content": "two"}, 3),
    ])
    # negative offset counts from the end
    assert "Showing 3-3" in read_thread(tid, summary=True, offset=-1)
    # offset past the end is reported
    assert "past the end" in read_thread(tid, summary=True, offset=99)


def test_summary_toc_tool_only_step_preview(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 2),
    ])
    out = read_thread(tid, summary=True)
    assert "[tool use]" in out  # an assistant step with no text previews as tool use


# ── resolve edge: an ImportState watermark whose Thread is gone ────────────────

def test_read_unknown_ref_reports_not_found(archive_home) -> None:
    init_db()
    assert "not found" in read_thread("nope-nope")


def test_structured_unknown_ref_is_empty(archive_home) -> None:
    init_db()
    assert read_thread_structured("nope-nope")["messages"] == []


# ── focused read: a huge preceding context turn must not eat the budget ────────

def test_around_event_drops_oversized_preceding_context(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "X" * 4000}, 1),   # turn 1: huge context
        ("text_complete", {"text": "Y" * 4000}, 1),
        ("user_message_sent", {"content": "the focused question"}, 2),  # turn 2: match
        ("text_complete", {"text": "focused reply"}, 2),
    ])
    # context_turns=1 would include the huge turn 1; a tight budget forces the
    # retry that drops the preceding context and re-accumulates from the focus.
    out = read_thread(tid, around_event=3, context_turns=1, max_chars=200)
    assert "the focused question" in out
    assert "focused reply" in out


# ══════════════════════════════════════════════════════════════════════════════
# read.py — read_thread_structured (web viewer) branches
# ══════════════════════════════════════════════════════════════════════════════

def _stypes(msgs):
    return [(m["role"], b["type"]) for m in msgs for b in m["blocks"]]


def test_structured_thinking_and_tool_toggles(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("thinking_complete", {"text": "private reasoning"}, 2),
        ("text_complete", {"text": "public answer"}, 3),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 4),
        ("tool_execution_completed", {"output": "Z" * 25000}, 5),
        ("tool_execution_error", {"error": "kaboom"}, 6),
    ])
    # Everything off: thinking + tool blocks drop, only user + assistant text stay.
    lean = read_thread_structured(tid, include_thinking=False, include_tools=False)["messages"]
    lean_types = {t for _r, t in _stypes(lean)}
    assert "thinking" not in lean_types
    assert not ({"tool_use", "tool_result", "tool_error"} & lean_types)
    assert "text" in lean_types
    # Everything on: thinking + tool blocks present; the huge result flags truncation.
    full = read_thread_structured(tid, include_thinking=True, include_tools=True)["messages"]
    blocks = [b for m in full for b in m["blocks"]]
    assert any(b["type"] == "thinking" for b in blocks)
    tr = next(b for b in blocks if b["type"] == "tool_result")
    assert tr["truncated"] is True and len(tr["output"]) == 20000
    assert any(b["type"] == "tool_error" and b["error"] == "kaboom" for b in blocks)


def test_structured_messages_carry_their_event_ids(archive_home) -> None:
    # The viewer resolves a search hit's event id to its message for deep-link +
    # highlight — every rendered block's source event must be named on its message.
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("text_complete", {"text": "public answer"}, 2),
    ])
    msgs = read_thread_structured(tid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    all_ids = [eid for m in msgs for eid in m["event_ids"]]
    assert len(all_ids) == 2 and len(set(all_ids)) == 2
    assert all(isinstance(eid, int) for eid in all_ids)


def test_structured_empty_text_and_context_summary_variants(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "   "}, 1),   # empty user → no block
        ("user_message_sent", {"content": "q"}, 1),
        ("text_complete", {"text": "   "}, 2),   # empty → no block
        ("context_summary", {"content": "  "}, 3),  # empty → no block
        ("context_summary", {"content": "ordinary rolling summary"}, 4),
        ("context_summary", {"content": "The safeguards flagged and retried this."}, 5),
        ("text_complete", {"text": "real"}, 6),
    ])
    msgs = read_thread_structured(tid)["messages"]
    types = {t for _r, t in _stypes(msgs)}
    assert "context_summary" in types
    assert "safeguard_notice" in types
    # the whitespace-only text_complete produced no block
    texts = [b.get("text") for m in msgs for b in m["blocks"]]
    assert "   " not in texts


def test_structured_model_change_marker(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("model_change", {"to": "claude-opus-4-8"}, 2),
        ("model_change", {}, 3),   # no target → dropped
        ("text_complete", {"text": "ok"}, 4),
    ])
    msgs = read_thread_structured(tid)["messages"]
    switches = [b for m in msgs for b in m["blocks"] if b["type"] == "model_switch"]
    assert len(switches) == 1
    assert switches[0]["kind"] == "user" and switches[0]["to_model"] == "claude-opus-4-8"


def test_structured_fallback_content_block_becomes_model_switch(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("content_block", {"block_type": "fallback",
                           "data": {"raw": {"from": {"model": "sonnet"},
                                            "to": {"model": "opus"}}}}, 2),
        ("text_complete", {"text": "answered on opus"}, 3),
    ])
    # A fallback marker surfaces as a model_switch even with tools off (genuine ctx).
    msgs = read_thread_structured(tid, include_tools=False)["messages"]
    sw = [b for m in msgs for b in m["blocks"] if b["type"] == "model_switch"]
    assert sw and sw[0]["kind"] == "fallback"
    assert sw[0]["from_model"] == "sonnet" and sw[0]["to_model"] == "opus"


def test_structured_splits_turn_at_each_inference(archive_home) -> None:
    # One assistant turn, two model inferences (api_request boundaries) → two
    # messages, each carrying its own model, not one merged bubble.
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("api_request_started", {"model": "m1"}, 2),
        ("text_complete", {"text": "first inference"}, 2),
        ("api_request_completed", {"model": "m1", "input_tokens": 5, "output_tokens": 3,
                                   "stop_reason": "end_turn"}, 2),
        ("api_request_started", {"model": "m2"}, 3),
        ("text_complete", {"text": "second inference"}, 3),
        ("api_request_completed", {"model": "m2"}, 3),
    ])
    msgs = read_thread_structured(tid)["messages"]
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert len(asst) == 2
    assert asst[0]["meta"]["models"] == ["m1"]
    assert asst[0]["meta"]["tokens"]["input"] == 5
    assert asst[1]["meta"]["models"] == ["m2"]


# ══════════════════════════════════════════════════════════════════════════════
# _extract.py
# ══════════════════════════════════════════════════════════════════════════════

def test_block_search_text_variants() -> None:
    assert _block_search_text("just a string") == "just a string"       # str passthrough
    assert _block_search_text(42) == "42"                               # non-dict → _to_str
    blob = "A" * 2000  # long, no early space → looks base64, skipped
    got = _block_search_text({
        "type": "image", "source": "shouldskip", "text": "keep me",
        "meta": {"k": "v"}, "blob": blob,
    })
    assert "keep me" in got
    assert '"k": "v"' in got or "'k': 'v'" in got  # nested dict stringified
    assert blob not in got and "shouldskip" not in got


def test_to_str_and_strip_frontmatter() -> None:
    assert _to_str("x") == "x"
    assert _to_str({"a": 1}) == '{"a": 1}'
    body = _strip_frontmatter("---\ntitle: hi\n---\nthe body")
    assert body == "the body"
    assert _strip_frontmatter("no frontmatter here") == "no frontmatter here"
    # opening --- but no close → returned unchanged
    assert _strip_frontmatter("---\nunterminated") == "---\nunterminated"


def test_fts_tool_use_paths() -> None:
    # mcp tool with a command → the tool_name becomes the first heredoc line
    out = _fts_tool_use({"tool_name": "mcp__x__run",
                         "input": {"command": "first line\nsecond line"}})
    assert out and out[0][2] == "first line"  # heredoc first line as the tool name
    # body keys get frontmatter-stripped; other keys survive as k=v metadata
    out2 = _fts_tool_use({"tool_name": "Write",
                          "input": {"content": "---\nfm: 1\n---\nreal content", "path": "/x"}})
    assert "real content" in out2[0][0]
    assert "path=/x" in out2[0][0]
    # metadata precedes body so the doc cap can't truncate small keys away
    assert out2[0][0].index("path=/x") < out2[0][0].index("real content")
    # no body key → k=v join
    out3 = _fts_tool_use({"tool_name": "Edit", "input": {"a": "1", "b": "2"}})
    assert "a=1" in out3[0][0] and "b=2" in out3[0][0]
    # multiple body keys are all indexed (command no longer loses to description)
    out5 = _fts_tool_use({"tool_name": "Bash",
                          "input": {"command": "rg needle src/", "description": "Search for needle"}})
    assert "rg needle src/" in out5[0][0] and "Search for needle" in out5[0][0]
    # non-dict input → str()
    out4 = _fts_tool_use({"tool_name": "Raw", "input": "plain string arg"})
    assert "plain string arg" in out4[0][0]
    # nothing indexable → empty
    assert _fts_tool_use({"tool_name": "", "input": None}) == []
    assert out[0][1] == "tool"


def test_fts_tool_completed_paths() -> None:
    assert _fts_tool_completed({"output": "ok output", "tool_name": "Bash"}) == \
        [("ok output", "tool_result", "Bash")]
    err = _fts_tool_completed({"is_error": True, "error": "it broke", "tool_name": "Bash"})
    assert err == [("it broke", "tool_error", "Bash")]
    # is_error but output present → output is the error text
    assert _fts_tool_completed({"is_error": True, "output": "stderr text"})[0][1] == "tool_error"
    # is_error with nothing → empty
    assert _fts_tool_completed({"is_error": True}) == []
    # no output at all → empty
    assert _fts_tool_completed({"tool_name": "Bash"}) == []


def test_extract_fts_content_dispatch() -> None:
    assert extract_fts_content("user_message_sent", {}) == []           # empty payload
    assert extract_fts_content("user_message_sent", {"content": ""}) == []  # empty content
    # a continuation-summary user message is tagged distinctly
    cont = extract_fts_content("user_message_sent",
                               {"content": "This session is being continued from a previous conversation..."})
    assert cont[0][1] == "continuation_summary"
    # api_request_completed unpacks thinking + text content blocks
    arc = extract_fts_content("api_request_completed", {"content_blocks": [
        {"type": "thinking", "thinking": "th"}, {"type": "text", "text": "tx"}]})
    assert ("th", "thinking", None) in arc and ("tx", "text", None) in arc
    assert extract_fts_content("text_complete", {"text": "hi"}) == [("hi", "text", None)]
    assert extract_fts_content("text_complete", {"text": ""}) == []
    assert extract_fts_content("thinking_complete", {"text": "t"}) == [("t", "thinking", None)]
    assert extract_fts_content("thinking_complete", {}) == []
    # tool dispatch routes through the shared helpers
    assert extract_fts_content("tool_use_complete",
                               {"tool_name": "Bash", "input": {"command": "ls"}})[0][1] == "tool"
    assert extract_fts_content("tool_execution_completed",
                               {"output": "done", "tool_name": "Bash"})[0][1] == "tool_result"
    assert extract_fts_content("tool_execution_error", {"error": "e", "tool_name": "B"}) == \
        [("e", "tool_error", "B")]
    assert extract_fts_content("tool_execution_error", {}) == []
    assert extract_fts_content("context_summary", {"content": "c"}) == [("c", "context_summary", None)]
    assert extract_fts_content("context_summary", {}) == []
    assert extract_fts_content("thread_message_sent", {"content": "m"}) == [("m", "user", None)]
    assert extract_fts_content("thread_message_sent", {}) == []
    # ide_context: file_path leads the indexed text
    ide = extract_fts_content("ide_context", {"file_path": "/a/b.py", "content": "sel"})
    assert ide[0][0].startswith("/a/b.py") and ide[0][1] == "ide_context"
    assert extract_fts_content("ide_context", {"content": "   "}) == []
    # content_block indexes its human-readable text under its block_type
    cb = extract_fts_content("content_block",
                             {"block_type": "custom", "data": {"text": "block words"}})
    assert cb == [("block words", "custom", None)]
    assert extract_fts_content("content_block", {"block_type": "x", "data": {"source": "b64"}}) == []
    # message: text preferred, else assembled from content_blocks
    m1 = extract_fts_content("message", {"role": "tool", "content": "plain text"})
    assert m1 == [("plain text", "tool", None)]
    m2 = extract_fts_content("message", {"role": "tool", "content": "",
                                         "content_blocks": [{"text": "from blocks"}]})
    assert "from blocks" in m2[0][0] and m2[0][1] == "tool"
    assert extract_fts_content("message", {"content": ""}) == []
    # a wholly unmodeled type indexes nothing
    assert extract_fts_content("some_new_type", {"content": "x"}) == []


# ══════════════════════════════════════════════════════════════════════════════
# _codex.py
# ══════════════════════════════════════════════════════════════════════════════

def test_codex_kind_parsing() -> None:
    assert codex_kind("codex_message") == "message"
    assert codex_kind("codex_") is None       # empty suffix
    assert codex_kind("plain") is None
    assert codex_kind(None) is None
    assert codex_kind(123) is None


def _render(kind, raw, seen=frozenset()):
    return render_codex_block(kind, {"raw": raw}, seen)


def test_codex_message_roleless_and_image() -> None:
    # No role → generic "message" label; an input_image chunk renders as a marker;
    # a non-dict chunk is skipped.
    label, text = _render("message", {"content": [
        "not a dict chunk",
        {"type": "input_image", "image_url": "data:..."},
        {"text": "spoken words"}]})
    assert label == "message"
    assert "[image]" in text and "spoken words" in text


def test_codex_search_and_marker_renderers() -> None:
    assert _render("tool_search_call", {"arguments": {"query": "find me"}}) == \
        ("tool search", "find me")
    # a nameless / non-dict tool entry is skipped; only named tools are listed
    assert _render("tool_search_output",
                   {"tools": [{"name": "grep"}, {"desc": "no name"}, "raw", {"name": "read"}]}) == \
        ("tool search result", "grep, read")
    assert _render("compacted", {"message": "history swapped"}) == \
        ("context compacted", "history swapped")
    assert _render("context_compacted", {}) == ("context compacted", "")
    assert _render("turn_aborted", {"reason": "user interrupt"}) == \
        ("turn aborted", "user interrupt")


def test_codex_machinery_hidden_and_raw_not_dict() -> None:
    assert render_codex_block("task_complete", {"raw": {"x": 1}}, frozenset()) is None
    # data.raw not a dict → treated as {}: context_compacted still renders.
    assert render_codex_block("context_compacted", {"raw": "notadict"}, frozenset()) == \
        ("context compacted", "")
    # data not a dict at all → raw defaults to {}.
    assert render_codex_block("turn_aborted", None, frozenset()) == ("turn aborted", "")


def test_codex_echo_is_hidden() -> None:
    # Text the modeled path already rendered is codex's duplicate transcript → hidden.
    assert _render("message", {"role": "assistant", "content": [{"text": "already shown"}]},
                   {"already shown"}) is None


def test_codex_unknown_kind_raw_dump_and_truncation() -> None:
    # An unmet kind pretty-prints its payload as JSON.
    label, text = _render("some_future_kind", {"detail": "hi"})
    assert label == "some future kind" and json.loads(text)["detail"] == "hi"
    # Oversized dump is truncated.
    _l, big = _render("future", {"blob": "z" * 5000})
    assert big.endswith("… (truncated)") and len(big) < 5000
    # A non-serialisable payload falls back to str() rather than raising.
    circular: dict = {}
    circular["me"] = circular
    _l2, text2 = _render("weird", circular)
    assert text2  # produced something, did not raise


# ══════════════════════════════════════════════════════════════════════════════
# fts.py
# ══════════════════════════════════════════════════════════════════════════════

def test_build_event_hit_timestamp_normalisation() -> None:
    # unparseable timestamp → None
    h = build_event_hit(event_id=1, thread_id=1, event_type="user_message_sent",
                        content_type="user", snippet="s", full_content="f",
                        occurred_at="not-a-timestamp")
    assert h["occurred_at"] is None
    # an offset-aware string is normalised to local-naive
    h2 = build_event_hit(event_id=2, thread_id=1, event_type="user_message_sent",
                         content_type="user", snippet="s", full_content="f",
                         occurred_at="2026-01-01T10:00:00+05:00")
    assert h2["occurred_at"] is not None and h2["occurred_at"].tzinfo is None


def test_search_events_filters_and_empty_or(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "alpha beta gamma"}, 1),
        ("text_complete", {"text": "assistant delta text"}, 2),
    ])
    rebuild_fts()
    # tool_name filter clause (no tool docs → empty, but the clause runs)
    assert search_events("alpha", tool_name="Bash") == []
    # exclude_content_types clause drops the user doc
    ex = search_events("alpha", exclude_content_types=["user"])
    assert all(h["content_type"] != "user" for h in ex)
    # since/until clauses
    assert search_events("alpha", since="2020-01-01", until="2030-01-01")
    assert search_events("alpha", until="2020-01-01") == []
    # a pipe-OR query that is all separators reduces to no terms → empty
    assert search_events("|", session=None) == []
    assert search_events("alpha", thread_id=tid)  # explicit thread scope path


def test_index_thread_meta_empty_scope_is_noop(archive_home) -> None:
    _seed([("user_message_sent", {"content": "hi"}, 1)])
    rebuild_fts()
    # an explicit empty thread-id scope writes nothing
    assert index_thread_meta(thread_ids=[]) == 0


def test_index_thread_meta_thread_without_events_has_no_anchor(archive_home) -> None:
    # A thread with a title but NO indexable events cannot be anchored, so its meta
    # doc is skipped rather than written danglingly.
    init_db()
    with use_session() as s:
        s.add(Thread(id=50, name="t50", title="Orphan Title No Events",
                     thread_type="conversation", source="claude-code",
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
    rebuild_fts()
    assert index_thread_meta(thread_ids=[50]) == 0
    assert not search("Orphan Title No Events", content_types=["title"])


def test_fts_status_reports_count(archive_home) -> None:
    _seed([
        ("user_message_sent", {"content": "searchable words here"}, 1),
        ("text_complete", {"text": "more searchable words"}, 2),
    ])
    rebuild_fts()
    st = fts_status()
    assert st["table"] == "event_search" and st["indexed"] >= 2


def test_stitch_delta_tuples_from_raw_deltas(archive_home) -> None:
    _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("thinking_delta", {"text": "think ", "block_index": 0}, 2, "call1"),
        ("thinking_delta", {"text": "hard", "block_index": 0}, 2, "call1"),
        ("text_delta", {"text": "final answer", "block_index": 1}, 3, "call1"),
    ])
    with get_session() as s:
        out = stitch_delta_tuples(s, "call1")
    by_type = {ct: content for content, ct, _tn, _anchor in out}
    assert by_type["thinking"] == "think hard"
    assert by_type["text"] == "final answer"


def test_gate_arc_tuples_branches(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        # a summary-less call: its text exists only as raw deltas
        ("text_delta", {"text": "stitched only", "block_index": 0}, 2, "arc1"),
    ])
    with get_session() as s:
        cache: dict = {}
        # api_call_id None → can't prove no twin → nothing
        assert _gate_arc_tuples(s, api_call_id=None, thread_id=tid, tuples=[],
                                twin_text_cache=cache) == []
        # precomputed twinned_calls containing the id → gated out
        assert _gate_arc_tuples(s, api_call_id="arc1", thread_id=tid, tuples=[("x", "text", None)],
                                twin_text_cache=cache, twinned_calls={"arc1"}) == []
        # no tuples → stitched from the call's raw deltas
        stitched = _gate_arc_tuples(s, api_call_id="arc1", thread_id=tid, tuples=[],
                                    twin_text_cache=cache, twinned_calls=set())
        assert stitched and stitched[0][0] == "stitched only"
        # a second gate over the same text is deduped away (seen set)
        again = _gate_arc_tuples(s, api_call_id="arc1", thread_id=tid,
                                 tuples=[("stitched only", "text", None)],
                                 twin_text_cache=cache, twinned_calls=set())
        assert again == []


def test_gate_arc_tuples_incremental_twin_query(archive_home) -> None:
    # No precomputed twinned_calls → the twin check is a live indexed query. A call
    # that HAS a granular twin gates out.
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("text_complete", {"text": "twinned text"}, 2, "hastwin"),
    ])
    with get_session() as s:
        assert _gate_arc_tuples(s, api_call_id="hastwin", thread_id=tid,
                                tuples=[("twinned text", "text", None)],
                                twin_text_cache={}) == []


def test_cached_twin_texts_evicts_when_oversized(archive_home) -> None:
    tid = _seed([
        ("user_message_sent", {"content": "q"}, 1),
        ("text_complete", {"text": "twin text body"}, 2),
    ], tid=100)
    with get_session() as s:
        cache = {n: set() for n in range(65)}  # over the 64-thread bound; tid 100 absent
        got = _cached_twin_texts(s, tid, cache)
        assert "twin text body" in got
        assert tid in cache  # the wholesale eviction happened, then this thread cached


def test_rebuild_indexes_orphan_delta_calls(archive_home) -> None:
    # A stream killed before its api_request_completed arrived: deltas with an
    # api_call_id but no summary and no twin. rebuild_fts stitches + indexes them,
    # anchored at the block's last delta event, so the text stays searchable.
    _seed([
        ("user_message_sent", {"content": "kickoff the orphan run"}, 1),
        ("text_delta", {"text": "orphaned assistant ", "block_index": 0}, 2, "orphancall"),
        ("text_delta", {"text": "prose survives", "block_index": 0}, 2, "orphancall"),
    ])
    count = rebuild_fts()
    assert count >= 1
    hits = search("prose survives")
    assert any("orphaned assistant prose survives" in (h["full_content"] or "") for h in hits)
