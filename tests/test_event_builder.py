"""The core NormalizedMessage → ThreadEvent contract of DefaultEventBuilder.

Every imported conversation flows through ``build_events``; these tests pin its
behavioral contract per role and per block type, plus the cross-cutting
invariants:

* **user turns** — user_message_sent shape, empty turns dropped, embedded
  tool_result blocks become tool_execution_completed / tool_execution_error
  (with the unpaired flag when the id is missing), ide_context survives as its
  own events, images land in the live multimodal payload shape, injected /
  queued tagging;
* **assistant turns** — the started → content events → completed → stream
  envelope, one api_call_id across the turn, block_index per block, the api
  summary blocks, the content_text fallback, and monotonic non-decreasing
  per-block timestamps;
* **system turns** — the block-type → event-type mapping (context_summary,
  file_snapshot, queue_operation, progress, generic fallback);
* **timestamps** — never silently fabricated: inherited from the previous turn
  (``inferred_prev_turn``) or loudly flagged (``fabricated_no_source``);
* **dedup keys** — timestamp-free content identity: re-import collapses,
  edited content forks, missing provider ids fall back to a content anchor.

(Unknown block types and non-standard roles are pinned separately in
test_event_builder_fidelity.py.)
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.event_builder import compute_dedup_key

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _user(text: str = "hello", *, msg_id: str = "u1", created_at: str | None = "2026-01-01T10:00:00Z",
          blocks: list | None = None, provider_data: dict | None = None) -> dict:
    msg: dict = {
        "role": "user",
        "provider_message_id": msg_id,
        "content_text": text,
        "content_blocks": blocks or [],
    }
    if created_at is not None:
        msg["created_at"] = created_at
    if provider_data is not None:
        msg["provider_data"] = provider_data
    return msg


def _assistant(blocks: list, *, msg_id: str = "a1", text: str = "", provider_data: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "provider_message_id": msg_id,
        "created_at": "2026-01-01T10:00:00Z",
        "content_text": text,
        "content_blocks": blocks,
        "provider_data": provider_data or {"model": "m"},
    }


# ── user turns ────────────────────────────────────────────────────────────────
def test_user_message_basic() -> None:
    events = DefaultEventBuilder().build_events(_user("hello"), "stream-1")
    assert [e.event_type for e in events] == ["user_message_sent"]
    ev = events[0]
    assert ev.payload["content"] == "hello"
    assert ev.payload["provider_data"] == {"provider_message_id": "u1"}
    assert ev.stream_id == "stream-1"
    assert ev.occurred_at == T0
    assert "timestamp_inferred" not in ev.payload  # a real source timestamp is unflagged
    assert ev.dedup_key is not None and ev.dedup_key.startswith("u1:user_message_sent::")


def test_empty_user_message_produces_no_events() -> None:
    assert DefaultEventBuilder().build_events(_user("   "), "s") == []


def test_user_tool_result_blocks_become_tool_execution_events() -> None:
    """CC JSONL shape: tool results arrive as user-role turns with tool_result
    blocks and no user text — no user_message_sent, one event per result, and an
    error result gets the distinct tool_execution_error type (the display reader
    only recognizes failure via the event type)."""
    blocks = [
        {"type": "tool_result", "tool_use_id": "t1", "name": "Bash", "content": "ok output"},
        {"type": "tool_result", "tool_use_id": "t2", "name": "Read", "content": "boom", "is_error": True},
    ]
    events = DefaultEventBuilder().build_events(_user("", blocks=blocks), "s")
    assert [e.event_type for e in events] == ["tool_execution_completed", "tool_execution_error"]

    ok, err = events
    assert ok.payload == {"tool_call_id": "t1", "tool_name": "Bash", "output": "ok output", "is_error": False}
    assert err.payload == {"tool_call_id": "t2", "tool_name": "Read", "error": "boom"}
    # dedup keys are namespaced by the tool call id, so two results on one turn don't collide
    assert ":tool=t1:" in ok.dedup_key and ":tool=t2:" in err.dedup_key


def test_tool_result_without_id_is_flagged_unpaired() -> None:
    blocks = [{"type": "tool_result", "name": "Bash", "content": "orphan"}]
    (ev,) = DefaultEventBuilder().build_events(_user("", blocks=blocks), "s")
    assert ev.event_type == "tool_execution_completed"
    assert ev.payload["unpaired"] is True  # never a minted uuid that dangles


def test_tool_load_confirmation_becomes_tool_loaded_not_user_message() -> None:
    blocks = [{"type": "tool_result", "tool_use_id": "t1", "name": "skill", "content": "doc"}]
    events = DefaultEventBuilder().build_events(_user("Tool loaded.", blocks=blocks), "s")
    assert [e.event_type for e in events] == ["tool_loaded"]
    assert events[0].payload["content"] == "Tool loaded."


def test_ide_context_blocks_survive_as_events_even_on_a_context_only_turn() -> None:
    blocks = [
        {"type": "ide_context", "context_type": "opened_file", "raw_content": "<f>a.py</f>",
         "file_path": "/w/a.py", "seq": 0},
        {"type": "ide_context", "context_type": "selection", "raw_content": "x = 1", "seq": 1},
    ]
    events = DefaultEventBuilder().build_events(_user("", blocks=blocks), "s")
    assert [e.event_type for e in events] == ["ide_context", "ide_context"]
    first, second = events
    assert first.payload["context_type"] == "opened_file"
    assert first.payload["file_path"] == "/w/a.py"
    assert first.payload["content"] == "<f>a.py</f>"
    assert "file_path" not in second.payload
    # block_index (the seq) namespaces the dedup key: two context blocks, two keys
    assert first.dedup_key != second.dedup_key
    assert ":blk=0:" in first.dedup_key and ":blk=1:" in second.dedup_key


def test_user_images_land_in_the_live_multimodal_shape() -> None:
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},  # not base64 → not extracted
    ]
    (ev,) = DefaultEventBuilder().build_events(_user("see pic", blocks=blocks), "s")
    assert ev.event_type == "user_message_sent"
    assert ev.payload["content_type"] == "multimodal"
    assert ev.payload["images"] == [{"media_type": "image/jpeg", "data": "AAAA"}]


def test_injected_content_is_tagged_with_its_source() -> None:
    b = DefaultEventBuilder()
    cases = {
        "Caveat: the messages below were generated": "caveat",
        "This session is being continued from a previous conversation.": "context_continuation",
        "Warmup": "warmup",
        "note\n<task-notification>done</task-notification>": "task_notification",
        "<session_briefing>morning</session_briefing> hi": "session_briefing",
    }
    for content, source in cases.items():
        (ev,) = b.build_events(_user(content), "s")
        assert ev.payload.get("injected") is True, content
        assert ev.payload.get("source") == source

    # Negative space: an ordinary message starting with "Warmup" but long is real,
    # and a session_briefing tag past the 200-char head doesn't count.
    (ev,) = b.build_events(_user("Warmup laps before the real question: what is thread?"), "s")
    assert "injected" not in ev.payload
    (ev,) = b.build_events(_user("x" * 300 + "<session_briefing>"), "s")
    assert "injected" not in ev.payload


def test_queued_steering_message_is_tagged() -> None:
    (ev,) = DefaultEventBuilder().build_events(
        _user("wait, stop", provider_data={"queued_command": True}), "s"
    )
    assert ev.payload["queued"] is True


# ── assistant turns ───────────────────────────────────────────────────────────
def test_assistant_turn_emits_the_full_envelope() -> None:
    blocks = [
        {"type": "thinking", "text": "hmm"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
    ]
    provider_data = {"model": "claude-x", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 2}}
    events = DefaultEventBuilder().build_events(
        _assistant(blocks, provider_data=provider_data), "s", api_call_id="call-1"
    )
    assert [e.event_type for e in events] == [
        "api_request_started", "thinking_complete", "text_complete",
        "tool_use_complete", "api_request_completed", "stream_completed",
    ]
    assert all(e.api_call_id == "call-1" for e in events)

    started, thinking, text, tool_use, completed, _stream = events
    assert started.payload["model"] == "claude-x"
    assert thinking.payload == {"text": "hmm", "block_index": 0}
    assert text.payload == {"text": "answer", "block_index": 1}
    assert tool_use.payload == {
        "tool_call_id": "t1", "tool_name": "Bash", "input": {"command": "ls"}, "block_index": 2,
    }
    # The api summary reproduces the turn in API shape, with usage + stop_reason.
    assert completed.payload["content_blocks"] == [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "answer"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
    ]
    assert completed.payload["stop_reason"] == "tool_use"
    assert (completed.payload["input_tokens"], completed.payload["output_tokens"],
            completed.payload["thinking_tokens"]) == (10, 5, 2)


def test_assistant_completed_preserves_cost_and_extra_usage_fields() -> None:
    # A pay-per-token source (cloth) records per-message cost + cache-token counts; none
    # of it may be dropped from the api_request_completed summary.
    provider_data = {
        "model": "deepseek/deepseek-v4-pro", "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 0,
                  "cache_read_tokens": 80, "cache_write_tokens": 5},
        "cost": 0.012075,
    }
    events = DefaultEventBuilder().build_events(
        _assistant([{"type": "text", "text": "hi"}], provider_data=provider_data), "s"
    )
    completed = next(e for e in events if e.event_type == "api_request_completed")
    assert completed.payload["cost"] == 0.012075
    assert completed.payload["cache_read_tokens"] == 80
    assert completed.payload["cache_write_tokens"] == 5
    # the flat trio stays for readers that only know the old shape
    assert (completed.payload["input_tokens"], completed.payload["output_tokens"]) == (100, 20)


def test_assistant_completed_omits_cost_when_absent() -> None:
    # A subscription transcript (Claude Code) has no per-message cost — the payload stays
    # cost-free rather than carrying a null.
    provider_data = {"model": "claude-x", "usage": {"input_tokens": 10, "output_tokens": 5}}
    events = DefaultEventBuilder().build_events(
        _assistant([{"type": "text", "text": "hi"}], provider_data=provider_data), "s"
    )
    completed = next(e for e in events if e.event_type == "api_request_completed")
    assert "cost" not in completed.payload


def test_assistant_generates_one_shared_api_call_id_when_none_given() -> None:
    events = DefaultEventBuilder().build_events(_assistant([{"type": "text", "text": "hi"}]), "s")
    ids = {e.api_call_id for e in events}
    assert len(ids) == 1 and None not in ids


def test_assistant_content_text_fallback_when_no_blocks() -> None:
    events = DefaultEventBuilder().build_events(_assistant([], text="plain answer"), "s")
    assert [e.event_type for e in events] == [
        "api_request_started", "text_complete", "api_request_completed", "stream_completed",
    ]
    assert events[1].payload == {"text": "plain answer", "block_index": 0}
    assert events[2].payload["content_blocks"] == [{"type": "text", "text": "plain answer"}]


def test_assistant_raw_string_block_is_text() -> None:
    events = DefaultEventBuilder().build_events(_assistant(["bare string"]), "s")
    assert events[1].event_type == "text_complete"
    assert events[1].payload == {"text": "bare string", "block_index": 0}


def test_assistant_tool_use_without_id_is_flagged_unpaired() -> None:
    events = DefaultEventBuilder().build_events(
        _assistant([{"type": "tool_use", "name": "Bash", "input": {}}]), "s"
    )
    tool = next(e for e in events if e.event_type == "tool_use_complete")
    assert tool.payload["unpaired"] is True


def test_assistant_embedded_tool_result_is_an_event_not_an_api_block() -> None:
    blocks = [
        {"type": "text", "text": "ran it"},
        {"type": "tool_result", "tool_use_id": "t1", "name": "Bash", "content": "out"},
    ]
    events = DefaultEventBuilder().build_events(_assistant(blocks), "s")
    assert [e.event_type for e in events] == [
        "api_request_started", "text_complete", "tool_execution_completed",
        "api_request_completed", "stream_completed",
    ]
    completed = events[-2]
    # tool_result is not part of the assistant's API-shaped summary
    assert [b["type"] for b in completed.payload["content_blocks"]] == ["text"]


def test_assistant_block_timestamps_are_monotonic_non_decreasing() -> None:
    """A block's start_timestamp refines the running time forward, never backward,
    and the turn's completed/stream events carry the running max."""
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "thinking", "text": "later", "start_timestamp": "2026-01-01T10:00:05Z"},
        {"type": "text", "text": "out-of-order", "start_timestamp": "2026-01-01T10:00:03Z"},
    ]
    events = DefaultEventBuilder().build_events(_assistant(blocks), "s")
    t5 = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    started, first, later, out_of_order, completed, stream = events
    assert started.occurred_at == T0
    assert first.occurred_at == T0
    assert later.occurred_at == t5
    assert out_of_order.occurred_at == t5  # clamped forward, not moved back
    assert completed.occurred_at == t5 and stream.occurred_at == t5


# ── system turns ──────────────────────────────────────────────────────────────
def test_system_block_type_to_event_type_mapping() -> None:
    b = DefaultEventBuilder()

    def system(blocks, provider_data=None, text=""):
        return b.build_events(
            {"role": "system", "provider_message_id": "s1", "created_at": "2026-01-01T10:00:00Z",
             "content_text": text, "content_blocks": blocks, "provider_data": provider_data or {}},
            "s",
        )

    (ev,) = system([{"type": "context_summary", "text": "compacted", "summarizes": ["e1", "e2"]}],
                   provider_data={"summary_type": "compaction"})
    assert ev.event_type == "context_summary"
    assert ev.payload["content"] == "compacted"
    assert ev.payload["summary_type"] == "compaction"
    assert ev.payload["summarizes"] == ["e1", "e2"]

    (ev,) = system([{"type": "file_snapshot", "files": [{"path": "a.py"}], "timestamp": "ts"}])
    assert ev.event_type == "file_snapshot"
    assert ev.payload["files"] == [{"path": "a.py"}]
    assert ev.payload["snapshot_timestamp"] == "ts"

    (ev,) = system([{"type": "queue_operation", "data": {"op": "enqueue"}}])
    assert ev.event_type == "queue_operation"
    assert ev.payload["op"] == "enqueue"  # block data verbatim

    (ev,) = system([{"type": "progress", "data": {"pct": 50}, "tool_use_id": "t1"}])
    assert ev.event_type == "progress"
    assert ev.payload == {"data": {"pct": 50}, "tool_use_id": "t1"}

    # A system block type the builder doesn't model is preserved generically.
    (ev,) = system([{"type": "mystery_meta"}], text="raw system text")
    assert ev.event_type == "context_summary"
    assert ev.payload["system_type"] == "mystery_meta"
    assert ev.payload["content"] == "raw system text"

    assert system([]) == []  # a blockless system turn emits nothing


# ── timestamps: never silently fabricated ─────────────────────────────────────
def test_missing_timestamp_inherits_prev_turn_and_is_flagged() -> None:
    events = DefaultEventBuilder().build_events(
        _user("hi", created_at=None), "s", prev_occurred_at=T0
    )
    (ev,) = events
    assert ev.occurred_at == T0  # inherited, monotonic — not invented
    assert ev.payload["timestamp_inferred"] is True
    assert ev.payload["timestamp_source"] == "inferred_prev_turn"


def test_missing_timestamp_with_no_prev_is_loudly_fabricated() -> None:
    (ev,) = DefaultEventBuilder().build_events(_user("hi", created_at=None), "s")
    assert ev.payload["timestamp_inferred"] is True
    assert ev.payload["timestamp_source"] == "fabricated_no_source"
    assert ev.occurred_at.tzinfo is not None  # aware, comparable in the ordering path


def test_provenance_flags_every_event_of_an_assistant_turn() -> None:
    events = DefaultEventBuilder().build_events(
        {"role": "assistant", "provider_message_id": "a1", "content_text": "hi",
         "content_blocks": [], "provider_data": {"model": "m"}},
        "s", prev_occurred_at=T0,
    )
    assert len(events) == 4
    assert all(e.payload["timestamp_source"] == "inferred_prev_turn" for e in events)
    assert all(e.occurred_at == T0 for e in events)


# ── dedup keys: timestamp-free content identity ───────────────────────────────
def test_dedup_key_is_stable_across_reimport_and_timestamp_provenance() -> None:
    """Same provider id + same content → same key, even when one import had a real
    timestamp and the other inherited one (provenance flags are not identity)."""
    b = DefaultEventBuilder()
    (with_ts,) = b.build_events(_user("hello"), "s")
    (inherited,) = b.build_events(_user("hello", created_at=None), "s", prev_occurred_at=T0)
    assert with_ts.dedup_key == inherited.dedup_key


def test_dedup_key_forks_on_edited_content() -> None:
    b = DefaultEventBuilder()
    (v1,) = b.build_events(_user("hello"), "s")
    (v2,) = b.build_events(_user("hello, edited"), "s")  # same provider_message_id
    assert v1.dedup_key != v2.dedup_key  # both versions of the turn are kept


def test_dedup_key_without_provider_id_anchors_on_content() -> None:
    key = compute_dedup_key("", "user_message_sent", {"content": "hi"})
    anchor, event_type, block, content_hash = key.split(":")
    assert anchor == f"c={content_hash}"
    assert event_type == "user_message_sent" and block == ""


def test_every_built_event_carries_a_dedup_key() -> None:
    events = DefaultEventBuilder().build_events(
        _assistant([{"type": "thinking", "text": "t"}, {"type": "text", "text": "x"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
        "s",
    )
    assert all(e.dedup_key for e in events)
    assert len({e.dedup_key for e in events}) == len(events)  # and they're distinct


def test_thread_event_occurred_at_default_is_utc_aware() -> None:
    from thread_archive._thread_import.event_builder import ThreadEvent

    ev = ThreadEvent(event_type="x", payload={}, stream_id="s")
    assert ev.occurred_at.tzinfo is not None
