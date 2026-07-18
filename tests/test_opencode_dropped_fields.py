"""OpenCode importer: source fields that must survive into events.

Covers the assistant message extras (cost, tokens, finish, error), tool-part
error/metadata, the synthetic text-part flag, and session project_id — plus the
invariant that annotation-only enrichment never changes an event's dedup key.
"""

from thread_archive._importers.opencode import (
    _build_opencode_messages,
    _opencode_to_normalized,
)
from thread_archive._thread_import import DefaultEventBuilder


def _assistant_msg(msg_id="msg_a1", **extra):
    data = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude-fable-5",
        "time": {"created": 1752700000000, "completed": 1752700005000},
        "cost": 0.031076,
        "tokens": {
            "total": 15274,
            "input": 15142,
            "output": 132,
            "reasoning": 7,
            "cache": {"read": 900, "write": 40},
        },
        "finish": "stop",
    }
    data.update(extra)
    return (msg_id, data)


def _normalize(messages, parts_by_message):
    norm = _build_opencode_messages(messages, parts_by_message)
    return [_opencode_to_normalized(m) for m in norm]


def _events_for(messages, parts_by_message):
    events = []
    for nm in _normalize(messages, parts_by_message):
        events.extend(DefaultEventBuilder().build_events(nm, "stream-1"))
    return events


def _by_type(events, event_type):
    return [e for e in events if e.event_type == event_type]


def test_cost_usage_stop_reason_on_api_request_completed():
    events = _events_for(
        [_assistant_msg()],
        {"msg_a1": [{"type": "text", "text": "hi", "time": {"start": 1752700001000}}]},
    )
    (completed,) = _by_type(events, "api_request_completed")
    p = completed.payload
    assert p["cost"] == 0.031076
    assert p["input_tokens"] == 15142
    assert p["output_tokens"] == 132
    assert p["thinking_tokens"] == 7
    assert p["cache_read_tokens"] == 900
    assert p["cache_write_tokens"] == 40
    assert "total" not in p
    assert p["stop_reason"] == "stop"


def test_incomplete_marker_wins_over_finish():
    # An abandoned turn keeps stop_reason="incomplete" even if a finish value
    # is somehow present.
    msg = {
        "id": "msg_x",
        "role": "assistant",
        "started_at": 1752700000000,
        "completed_at": None,
        "model": "anthropic/claude-fable-5",
        "segments": [{"kind": "text", "text": "partial", "ts": None}],
        "incomplete": True,
        "finish": "stop",
        "cost": None,
        "tokens": None,
        "error": None,
    }
    nm = _opencode_to_normalized(msg)
    assert nm["provider_data"]["stop_reason"] == "incomplete"


def test_error_lands_as_annotation():
    err = {
        "name": "UnknownError",
        "data": {"message": "Upstream error", "code": 502, "nested": {"drop": "me"}},
    }
    events = _events_for(
        [_assistant_msg(error=err)],
        {"msg_a1": [{"type": "text", "text": "hi"}]},
    )
    (completed,) = _by_type(events, "api_request_completed")
    ann = completed.payload["annotations"]["error"]
    assert ann == {"name": "UnknownError", "message": "Upstream error", "code": 502}
    # NOT the payload-level content "error" key.
    assert "error" not in completed.payload


def test_tool_error_text_becomes_result_content():
    part = {
        "type": "tool",
        "callID": "call_1",
        "tool": "webfetch",
        "state": {
            "status": "error",
            "input": {"url": "https://x"},
            "error": "StatusCode: non 2xx status code (404)",
            "time": {"start": 1752700001000, "end": 1752700002000},
        },
    }
    events = _events_for([_assistant_msg()], {"msg_a1": [part]})
    (err_ev,) = _by_type(events, "tool_execution_error")
    assert err_ev.payload["error"] == "StatusCode: non 2xx status code (404)"


def test_tool_metadata_and_title_become_block_annotations():
    part = {
        "type": "tool",
        "callID": "call_2",
        "tool": "bash",
        "state": {
            "status": "completed",
            "input": {"command": "ls"},
            "output": "files",
            "title": "list files",
            "metadata": {"exit": 0, "truncated": False},
            "time": {"start": 1752700001000, "end": 1752700002000},
        },
    }
    events = _events_for([_assistant_msg()], {"msg_a1": [part]})
    (done,) = _by_type(events, "tool_execution_completed")
    assert done.payload["output"] == "files"
    assert done.payload["annotations"] == {
        "exit": 0,
        "truncated": False,
        "title": "list files",
    }


def test_synthetic_text_annotated():
    parts = [
        {"type": "text", "text": "<system-reminder>note</system-reminder>", "synthetic": True},
        {"type": "text", "text": "real answer"},
    ]
    events = _events_for([_assistant_msg()], {"msg_a1": parts})
    texts = _by_type(events, "text_complete")
    synthetic = [e for e in texts if e.payload.get("annotations") == {"synthetic": True}]
    plain = [e for e in texts if "annotations" not in e.payload]
    assert len(synthetic) == 1 and synthetic[0].payload["text"].startswith("<system-reminder>")
    assert len(plain) == 1 and plain[0].payload["text"] == "real answer"


def test_annotations_do_not_change_dedup_keys():
    parts_plain = [
        {"type": "text", "text": "answer"},
        {
            "type": "tool",
            "callID": "call_3",
            "tool": "bash",
            "state": {"status": "completed", "input": {}, "output": "out"},
        },
    ]
    parts_annotated = [
        {"type": "text", "text": "answer", "synthetic": True},
        {
            "type": "tool",
            "callID": "call_3",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {},
                "output": "out",
                "title": "t",
                "metadata": {"exit": 0, "truncated": True},
            },
        },
    ]
    err = {"name": "E", "data": {"message": "m"}}
    plain = _events_for([_assistant_msg()], {"msg_a1": parts_plain})
    annotated = _events_for(
        [_assistant_msg(error=err)], {"msg_a1": parts_annotated}
    )
    keys_plain = [(e.event_type, e.dedup_key) for e in plain]
    keys_annotated = [(e.event_type, e.dedup_key) for e in annotated]
    assert keys_plain == keys_annotated


def test_cost_and_usage_do_not_change_dedup_keys():
    bare = (
        "msg_a1",
        {
            "role": "assistant",
            "providerID": "anthropic",
            "modelID": "claude-fable-5",
            "time": {"created": 1752700000000, "completed": 1752700005000},
        },
    )
    rich = _assistant_msg()
    parts = {"msg_a1": [{"type": "text", "text": "hi"}]}
    keys_bare = [(e.event_type, e.dedup_key) for e in _events_for([bare], parts)]
    keys_rich = [(e.event_type, e.dedup_key) for e in _events_for([rich], parts)]
    assert keys_bare == keys_rich
