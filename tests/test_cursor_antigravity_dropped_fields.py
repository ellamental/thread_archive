"""Cursor and Antigravity importers must not silently drop source fields.

Cursor: per-bubble ``tokenCount`` / ``modelInfo`` / ``usageUuid`` / attached code
chunks / compaction summaries. Antigravity: ``thinking`` on planner steps (including
thinking-only steps), ``error`` text on ERROR_MESSAGE steps, ``truncated_fields``.
Annotation-only extras must never perturb dedup identity.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers import import_antigravity_session_incremental
from thread_archive._importers.cursor import (
    _cursor_to_normalized,
    import_cursor_from_payload,
)
from thread_archive._importers.antigravity import _build_antigravity_messages
from thread_archive._store import Event, get_session, init_db
from thread_archive._thread_import import DefaultEventBuilder


# ── helpers ──────────────────────────────────────────────────────────────────


def _events(event_type: str) -> list[dict]:
    with get_session() as s:
        return list(
            s.execute(
                select(Event.payload).where(Event.event_type == event_type)
            ).scalars().all()
        )


def _import_cursor(bubbles_by_id: dict[str, dict], composer_id: str = "comp1") -> None:
    init_db()
    composer_data = {
        "name": "Test Conversation",
        "lastUpdatedAt": 1700000100000,
        "fullConversationHeadersOnly": [{"bubbleId": bid} for bid in bubbles_by_id],
    }
    bubbles = {f"{composer_id}:{bid}": b for bid, b in bubbles_by_id.items()}
    import_cursor_from_payload(
        composer_id=composer_id, composer_data=composer_data, bubbles=bubbles
    )


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


# ── cursor ───────────────────────────────────────────────────────────────────


def test_cursor_usage_model_and_annotations_land(archive_home) -> None:
    """tokenCount → structured usage on api_request_completed; modelInfo.modelName
    replaces the hardcoded "cursor" model; usageUuid + attached code chunks land as
    annotations on the right message."""
    _import_cursor({
        "b1": {
            "type": 1, "text": "look at this file", "createdAt": 1700000000000,
            "tokenCount": {"inputTokens": 120, "outputTokens": 0},
            "usageUuid": "uu-user-1",
            "attachedCodeChunks": [{
                "relativeWorkspacePath": "src/app.py",
                "startLineNumber": 10,
                "lines": ["def main():", "    pass"],
            }],
            "attachedFileCodeChunksMetadataOnly": [{
                "relativeWorkspacePath": "src/other.py",
                "startLineNumber": 1,
                "lines": [],
            }],
        },
        "b2": {
            "type": 2, "text": "done", "createdAt": 1700000005000,
            "tokenCount": {"inputTokens": 45147, "outputTokens": 9280},
            "modelInfo": {"modelName": "claude-4.5-opus-high-thinking"},
            "usageUuid": "uu-asst-1",
        },
    })

    completed = _events("api_request_completed")
    assert len(completed) == 1
    c = completed[0]
    assert c["input_tokens"] == 45147
    assert c["output_tokens"] == 9280
    assert c["model"] == "claude-4.5-opus-high-thinking"
    assert c["annotations"]["usage_uuid"] == "uu-asst-1"

    started = _events("api_request_started")
    assert started[0]["model"] == "claude-4.5-opus-high-thinking"

    user = _events("user_message_sent")
    assert len(user) == 1
    ann = user[0]["annotations"]
    assert ann["usage_uuid"] == "uu-user-1"
    assert ann["usage"] == {"input_tokens": 120, "output_tokens": 0}
    attached = ann["attached_code"]
    assert attached[0] == {
        "path": "src/app.py", "start_line": 10, "text": "def main():\n    pass",
    }
    assert attached[1] == {
        "path": "src/other.py", "start_line": 1, "metadata_only": True,
    }


def test_cursor_model_falls_back_when_absent(archive_home) -> None:
    _import_cursor({
        "b1": {"type": 1, "text": "hi", "createdAt": 1700000000000},
        "b2": {"type": 2, "text": "hello", "createdAt": 1700000001000},
    })
    assert _events("api_request_completed")[0]["model"] == "cursor"


def test_cursor_compaction_summary_becomes_context_summary(archive_home) -> None:
    """A (cached)conversationSummary — stored as a JSON-encoded string — becomes a
    context_summary event instead of being dropped."""
    _import_cursor({
        "b1": {"type": 1, "text": "keep going", "createdAt": 1700000000000},
        "b2": {
            "type": 2, "text": "", "createdAt": 1700000001000,
            "cachedConversationSummary": json.dumps(
                {"summary": "Summary:\n1. The user asked for X."}
            ),
        },
    })
    summaries = _events("context_summary")
    assert len(summaries) == 1
    assert summaries[0]["content"] == "Summary:\n1. The user asked for X."
    assert summaries[0]["summary_type"] == "cursor_compaction"


def test_cursor_annotations_do_not_change_dedup_identity(archive_home) -> None:
    """Adding annotation-only extras (usageUuid, attached code, user usage) to a
    turn must leave every event's dedup key unchanged, so backfill can enrich
    already-stored events in place."""
    builder = DefaultEventBuilder()
    plain = {
        "id": "b1", "role": "user", "content": "hello", "created_at": 1700000000000,
    }
    enriched = {
        **plain,
        "usage": {"input_tokens": 9, "output_tokens": 0},
        "usage_uuid": "uu-1",
        "attached_code": [{"path": "a.py", "start_line": 1}],
    }
    keys_plain = [e.dedup_key for e in builder.build_events(_cursor_to_normalized(plain), "s1")]
    keys_rich = [e.dedup_key for e in builder.build_events(_cursor_to_normalized(enriched), "s1")]
    assert keys_plain == keys_rich

    asst_plain = {
        "id": "b2", "role": "assistant", "content": "hi", "created_at": 1700000001000,
    }
    asst_rich = {**asst_plain, "usage": {"input_tokens": 5, "output_tokens": 7}, "usage_uuid": "uu-2"}
    keys_plain = [e.dedup_key for e in builder.build_events(_cursor_to_normalized(asst_plain), "s1")]
    keys_rich = [e.dedup_key for e in builder.build_events(_cursor_to_normalized(asst_rich), "s1")]
    assert keys_plain == keys_rich

    # The model, by contrast, IS content-identity: a real model name changes keys.
    asst_model = {**asst_plain, "model": "claude-4.5-opus-high-thinking"}
    keys_model = [e.dedup_key for e in builder.build_events(_cursor_to_normalized(asst_model), "s1")]
    assert keys_model != keys_plain


# ── antigravity ──────────────────────────────────────────────────────────────


def test_antigravity_thinking_block_emitted(archive_home) -> None:
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>fix it</USER_REQUEST>"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z",
         "thinking": "The bug is in the loop bound.", "content": "On it."},
    ])
    r = import_antigravity_session_incremental(f, "ag-thinking")
    assert r.events_created > 0
    thinking = _events("thinking_complete")
    assert len(thinking) == 1
    assert thinking[0]["text"] == "The bug is in the loop bound."
    # Thinking precedes the text block within the turn.
    assert thinking[0]["block_index"] == 0
    assert _events("text_complete")[0]["text"] == "On it."


def test_antigravity_thinking_only_step_not_dropped(archive_home) -> None:
    """A step with thinking but no content and no tool_calls must still produce an
    assistant message carrying the thinking block."""
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>think about it</USER_REQUEST>"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z",
         "thinking": "Silent reasoning only.", "content": None},
    ])
    r = import_antigravity_session_incremental(f, "ag-thinking-only")
    assert r.events_created > 0
    thinking = _events("thinking_complete")
    assert len(thinking) == 1
    assert thinking[0]["text"] == "Silent reasoning only."
    assert len(_events("api_request_started")) == 1


def test_antigravity_error_text_taken_from_error_field(archive_home) -> None:
    """An ERROR_MESSAGE step with content null and the text only in `error` must
    persist that text on the tool_execution_error event, not a hollow one."""
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>run it</USER_REQUEST>"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z", "content": "running",
         "tool_calls": [{"name": "run_command", "args": {}}]},
        {"type": "ERROR_MESSAGE", "created_at": "2026-01-01T10:00:06Z",
         "content": None, "error": "command exited 1: permission denied"},
    ])
    r = import_antigravity_session_incremental(f, "ag-error")
    assert r.events_created > 0
    errors = _events("tool_execution_error")
    assert len(errors) == 1
    assert errors[0]["error"] == "command exited 1: permission denied"
    assert errors[0]["tool_call_id"] == "agtool-0"
    assert errors[0]["tool_name"] == "run_command"


def test_antigravity_truncated_fields_annotation(archive_home) -> None:
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>go</USER_REQUEST>"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z", "content": "clipped turn",
         "truncated_fields": ["tool_calls"]},
    ])
    r = import_antigravity_session_incremental(f, "ag-trunc")
    assert r.events_created > 0
    completed = _events("api_request_completed")
    assert completed[0]["annotations"]["truncated_fields"] == ["tool_calls"]


def test_antigravity_annotations_do_not_change_dedup_identity(archive_home) -> None:
    """truncated_fields is annotation-only: it must not perturb any dedup key."""
    builder = DefaultEventBuilder()

    def keys(lines):
        msgs = _build_antigravity_messages(lines, lines, "src")
        return [e.dedup_key for m in msgs for e in builder.build_events(m, "s1")]

    base = [{"source": "MODEL", "type": "PLANNER_RESPONSE",
             "created_at": "2026-01-01T10:00:05Z", "content": "hello"}]
    annotated = [dict(base[0], truncated_fields=["tool_calls"])]
    assert keys(base) == keys(annotated)
