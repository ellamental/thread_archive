"""A model switch mid-turn splits into separate messages so the switch is visible.

``read_thread_structured`` splits an assistant turn's tool loop at each api_request
boundary — one message per model inference — so a mid-turn model change lands on a
message boundary (and gets its own tint in the viewer) instead of hiding inside one
merged bubble. See ``read_thread_structured`` / the ``split`` flag in
``retrieval/read.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive.retrieval import read_thread_structured
from thread_archive.store import Event, Thread, init_db, use_session


def _dt(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 1, 1, 10, minute, second, tzinfo=timezone.utc)


def _seed(events, tid=1):
    """events: (event_type, payload, minute) tuples; ids assigned in list order."""
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="Model Switch", thread_type="conversation",
                     source="claude-code", source_id=f"proj:{tid}",
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload, minute) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


# One user turn whose tool loop starts on fable and escalates to opus mid-turn — the
# real shape captured when a Claude Code session auto-upgrades fable→opus partway
# through a turn (the whole turn used to collapse into one fable-tinted bubble).
_SWITCH = [
    ("user_message_sent", {"content": "do the thing"}, 1),
    ("api_request_started", {"model": "claude-fable-5"}, 1),
    ("text_complete", {"text": "starting on fable"}, 1),
    ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 1),
    ("api_request_completed", {"model": "claude-fable-5", "output_tokens": 5, "stop_reason": "tool_use"}, 1),
    ("tool_execution_completed", {"output": "a.txt"}, 1),
    ("api_request_started", {"model": "claude-opus-4-8"}, 2),  # <- the escalation
    ("text_complete", {"text": "finishing on opus"}, 2),
    ("api_request_completed", {"model": "claude-opus-4-8", "output_tokens": 8, "stop_reason": "end_turn"}, 2),
]


def test_model_switch_splits_into_separate_messages():
    tid = _seed(_SWITCH)
    msgs = read_thread_structured(tid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
    fable, opus = msgs[1], msgs[2]
    # each inference is its own message, carrying exactly its own model
    assert fable["meta"]["models"] == ["claude-fable-5"]
    assert opus["meta"]["models"] == ["claude-opus-4-8"]
    # the fable message owns its tool call + its result; opus owns the final text
    ftypes = [b["type"] for b in fable["blocks"]]
    assert "tool_use" in ftypes and "tool_result" in ftypes
    assert any(b["type"] == "text" and "finishing on opus" in b["text"] for b in opus["blocks"])
    # per-inference request tally (was one merged turn of 2 requests before the split)
    assert fable["meta"]["requests"] == 1 and opus["meta"]["requests"] == 1


def test_same_model_loop_still_splits_per_inference():
    # No model change, but two inferences → two messages (each a tool-call/response
    # iteration). The viewer regroups a same-model run visually; the data stays granular
    # so per-message model/token attribution is always exact.
    events = [
        ("user_message_sent", {"content": "go"}, 1),
        ("api_request_started", {"model": "claude-opus-4-8"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 1),
        ("api_request_completed", {"model": "claude-opus-4-8", "stop_reason": "tool_use"}, 1),
        ("tool_execution_completed", {"output": "ok"}, 1),
        ("api_request_started", {"model": "claude-opus-4-8"}, 2),
        ("text_complete", {"text": "done"}, 2),
        ("api_request_completed", {"model": "claude-opus-4-8", "stop_reason": "end_turn"}, 2),
    ]
    tid = _seed(events)
    msgs = read_thread_structured(tid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]
    assert all(m["meta"]["models"] == ["claude-opus-4-8"] for m in msgs[1:])


def _blocks(msgs):
    return [b for m in msgs for b in m["blocks"]]


# A safeguard-flagged message: Claude Code records a `fallback` content-block (from/to
# models) and a context_summary carrying the "Fable … safeguards flagged" reason.
_FALLBACK = [
    ("user_message_sent", {"content": "plan the sandbox"}, 1),
    ("api_request_started", {"model": "claude-fable-5"}, 1),
    ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 1),
    ("api_request_completed", {"model": "claude-fable-5", "stop_reason": "tool_use"}, 1),
    ("tool_execution_completed", {"output": "ok"}, 1),
    ("api_request_started", {"model": "claude-opus-4-8"}, 2),
    ("content_block", {"block_type": "fallback",
                       "data": {"raw": {"from": {"model": "claude-fable-5"},
                                        "to": {"model": "claude-opus-4-8"}}}}, 2),
    ("context_summary", {"content": "Fable 5's safeguards flagged this message. The "
                                    "safeguards are intentionally broad right now."}, 2),
    ("text_complete", {"text": "here's the plan"}, 2),
    ("api_request_completed", {"model": "claude-opus-4-8", "stop_reason": "end_turn"}, 2),
]


def test_fallback_becomes_first_class_model_switch_marker():
    tid = _seed(_FALLBACK)
    blocks = _blocks(read_thread_structured(tid)["messages"])
    switch = next(b for b in blocks if b["type"] == "model_switch")
    assert switch["kind"] == "fallback"
    assert switch["from_model"] == "claude-fable-5" and switch["to_model"] == "claude-opus-4-8"


def test_safeguard_context_summary_relabelled_as_notice():
    tid = _seed(_FALLBACK)
    blocks = _blocks(read_thread_structured(tid)["messages"])
    notice = next(b for b in blocks if b["type"] == "safeguard_notice")
    assert "safeguards flagged" in notice["text"]
    # …and it is NOT left as a generic context_summary
    assert not any(b["type"] == "context_summary" for b in blocks)


def test_switch_markers_survive_tools_off():
    # A model switch is conversation context, not machinery — it must show even when the
    # tools toggle strips tool calls/results.
    tid = _seed(_FALLBACK)
    blocks = _blocks(read_thread_structured(tid, include_tools=False)["messages"])
    types = {b["type"] for b in blocks}
    assert "model_switch" in types and "safeguard_notice" in types
    assert "tool_use" not in types and "tool_result" not in types


def test_manual_model_switch_is_a_standalone_divider():
    # A `/model` switch (model_change event) renders as its own `model_switch`-role
    # message between turns, carrying a user-kind switch marker with just the target.
    events = [
        ("user_message_sent", {"content": "first"}, 1),
        ("api_request_started", {"model": "claude-opus-4-8"}, 1),
        ("text_complete", {"text": "hi"}, 1),
        ("api_request_completed", {"model": "claude-opus-4-8", "stop_reason": "end_turn"}, 1),
        ("model_change", {"to": "claude-fable-5", "trigger": "user"}, 2),
        ("user_message_sent", {"content": "second"}, 3),
    ]
    tid = _seed(events)
    msgs = read_thread_structured(tid)["messages"]
    div = next(m for m in msgs if m["role"] == "model_switch")
    block = div["blocks"][0]
    assert block["type"] == "model_switch" and block["kind"] == "user"
    assert block["to_model"] == "claude-fable-5" and block["from_model"] is None
    # it sits between the two turns, not folded into either
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "model_switch", "user"]


def test_backfilled_model_change_slots_into_position():
    # Re-importing an older thread appends the model_change with a tail-end id but its
    # real (earlier) timestamp — it must render in its chronological slot, not last.
    events = [
        ("user_message_sent", {"content": "first"}, 1),
        ("api_request_started", {"model": "claude-opus-4-8"}, 1),
        ("text_complete", {"text": "hi"}, 1),
        ("api_request_completed", {"model": "claude-opus-4-8", "stop_reason": "end_turn"}, 1),
        ("user_message_sent", {"content": "second"}, 4),
        ("api_request_started", {"model": "claude-fable-5"}, 4),
        ("text_complete", {"text": "yo"}, 4),
        ("api_request_completed", {"model": "claude-fable-5", "stop_reason": "end_turn"}, 4),
        # backfilled last (highest id) but switched between the two turns (minute 2)
        ("model_change", {"to": "claude-fable-5", "trigger": "user"}, 2),
    ]
    tid = _seed(events)
    roles = [m["role"] for m in read_thread_structured(tid)["messages"]]
    # divider sits between the turns, not stranded at the end
    assert roles == ["user", "assistant", "model_switch", "user", "assistant"]


def test_provider_without_api_requests_keeps_one_message_per_turn():
    # No api_request events (e.g. a plain chat export) → nothing to split on; the whole
    # assistant turn stays a single contiguous message, exactly as before.
    events = [
        ("user_message_sent", {"content": "hi"}, 1),
        ("text_complete", {"text": "part one"}, 1),
        ("tool_use_complete", {"tool_name": "Bash", "input": {}}, 1),
        ("tool_execution_completed", {"output": "x"}, 1),
        ("text_complete", {"text": "part two"}, 1),
    ]
    tid = _seed(events)
    msgs = read_thread_structured(tid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert len(msgs[1]["blocks"]) == 4
