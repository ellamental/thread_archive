"""Claude Code source-field annotations: parser → builder → event payloads.

The CC parser carries source-line extras through the sanctioned annotations
channel (see event_builder's "Annotations" convention): message-level extras
land in ``provider_data["annotations"]`` and are copied by the builder onto
the anchor event's payload (``api_request_completed`` for assistant turns,
``user_message_sent`` for user turns); the line-level ``toolUseResult`` rides
as a block-level annotation on the tool_result block, surfacing on the
``tool_execution_completed`` / ``tool_execution_error`` payload. Annotations
are OUTSIDE the dedup content keys, so their presence must never change an
event's dedup identity — that invariant is what makes re-import idempotent
and backfill amendable, and it is pinned here.

Also pins the field-level drift ledger (``known_line_fields`` /
``known_message_fields`` on CLAUDE_CODE_CONFIG): an invented field on a known
line kind warns via validate_messages; a normal line does not.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser
from thread_archive._thread_import.parsers.validators import validate_messages


def _parse(lines: List[Dict[str, Any]]) -> List[dict]:
    return ClaudeCodeParser().parse_export({"lines": lines, "session_id": "s1"})


def _events(msg: dict) -> list:
    return DefaultEventBuilder().build_events(msg, "stream-1")


def _one(events: list, event_type: str):
    matches = [e for e in events if e.event_type == event_type]
    assert len(matches) == 1, f"expected one {event_type}, got {len(matches)}"
    return matches[0]


def _assistant_line(extras: Optional[Dict[str, Any]] = None,
                    message_extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    line: Dict[str, Any] = {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-01-01T10:00:05Z",
        "sessionId": "s1",
        "cwd": "/proj",
        "message": {
            "id": "msg_01",
            "role": "assistant",
            "model": "claude-opus-4",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "text", "text": "hi there"}],
            **(message_extras or {}),
        },
    }
    line.update(extras or {})
    return line


def _user_line(extras: Optional[Dict[str, Any]] = None,
               content: Any = "hello") -> Dict[str, Any]:
    line: Dict[str, Any] = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-01-01T10:00:00Z",
        "sessionId": "s1",
        "cwd": "/proj",
        "message": {"role": "user", "content": content},
    }
    line.update(extras or {})
    return line


# ── assistant-line annotations ───────────────────────────────────────────────


def test_assistant_extras_land_on_api_request_completed() -> None:
    extras = {
        "effort": "xhigh",
        "attributionMcpServer": "thread-lab",
        "attributionMcpTool": "backend_read",
        "attributionSkill": "db-query",
        "requestId": "req_123",
        "gitBranch": "main",
        "version": "2.5.0",
    }
    (msg,) = _parse([_assistant_line(extras, {"diagnostics": {"cache": "hit"}})])

    ann = msg["provider_data"]["annotations"]
    assert ann == {
        "effort": "xhigh",
        "attribution_mcp_server": "thread-lab",
        "attribution_mcp_tool": "backend_read",
        "attribution_skill": "db-query",
        "request_id": "req_123",
        "git_branch": "main",
        "version": "2.5.0",
        "message_id": "msg_01",
        "diagnostics": {"cache": "hit"},
    }
    # Pre-existing top-level provider_data extraction stays intact.
    assert msg["provider_data"]["request_id"] == "req_123"
    assert msg["provider_data"]["message_id"] == "msg_01"

    completed = _one(_events(msg), "api_request_completed")
    assert completed.payload["annotations"] == ann


def test_assistant_annotations_present_only() -> None:
    """A line without the extra fields produces NO annotations key — absent
    fields are never written as nulls."""
    (msg,) = _parse([_assistant_line({"gitBranch": None})])
    ann = msg["provider_data"].get("annotations", {})
    assert "git_branch" not in ann
    assert "effort" not in ann
    assert "diagnostics" not in ann
    completed = _one(_events(msg), "api_request_completed")
    assert "effort" not in completed.payload.get("annotations", {})


def test_assistant_annotations_do_not_change_dedup_key() -> None:
    plain = _one(_events(_parse([_assistant_line()])[0]), "api_request_completed")
    annotated = _one(
        _events(_parse([_assistant_line({"effort": "medium", "requestId": "r1"})])[0]),
        "api_request_completed",
    )
    assert plain.dedup_key == annotated.dedup_key
    assert annotated.payload["annotations"]["effort"] == "medium"


# ── user-line annotations ────────────────────────────────────────────────────


def test_user_extras_land_on_user_message_sent() -> None:
    extras = {
        "toolDenialKind": "prompt",
        "mcpMeta": {"server": "thread-lab"},
        "permissionMode": "acceptEdits",
        "origin": "cli",
        "promptSource": "terminal",
        "todos": [{"content": "fix it", "status": "pending"}],
        "thinkingMetadata": {"level": "high"},
    }
    (msg,) = _parse([_user_line(extras)])

    ann = msg["provider_data"]["annotations"]
    assert ann == {
        "tool_denial_kind": "prompt",
        "mcp_meta": {"server": "thread-lab"},
        "permission_mode": "acceptEdits",
        "origin": "cli",
        "prompt_source": "terminal",
        "todos": [{"content": "fix it", "status": "pending"}],
        "thinking_metadata": {"level": "high"},
    }
    sent = _one(_events(msg), "user_message_sent")
    assert sent.payload["annotations"] == ann


def test_user_annotations_do_not_change_dedup_key() -> None:
    plain = _one(_events(_parse([_user_line()])[0]), "user_message_sent")
    annotated = _one(
        _events(_parse([_user_line({"permissionMode": "plan", "origin": "cli"})])[0]),
        "user_message_sent",
    )
    assert plain.dedup_key == annotated.dedup_key
    assert annotated.payload["annotations"]["permission_mode"] == "plan"


# ── toolUseResult → block-level structured_result ────────────────────────────


def _tool_result_line(structured: Any, is_error: bool = False) -> Dict[str, Any]:
    return _user_line(
        extras={"toolUseResult": structured} if structured is not None else {},
        content=[{
            "type": "tool_result",
            "tool_use_id": "toolu_01",
            "content": [{"type": "text", "text": "42 lines"}],
            "is_error": is_error,
        }],
    )


def test_tool_use_result_attaches_to_tool_result_block() -> None:
    structured = {"type": "text", "file": {"filePath": "/a.py", "numLines": 42}}
    (msg,) = _parse([_tool_result_line(structured)])

    block = next(b for b in msg["content_blocks"] if b["type"] == "tool_result")
    assert block["annotations"] == {"structured_result": structured}

    completed = _one(_events(msg), "tool_execution_completed")
    assert completed.payload["annotations"] == {"structured_result": structured}
    # Content fields stay what they were — annotations are siblings.
    assert completed.payload["output"] == [{"type": "text", "text": "42 lines"}]


def test_tool_use_result_on_error_result() -> None:
    structured = {"interrupted": True}
    (msg,) = _parse([_tool_result_line(structured, is_error=True)])
    err = _one(_events(msg), "tool_execution_error")
    assert err.payload["annotations"] == {"structured_result": structured}


def test_tool_use_result_does_not_change_dedup_key() -> None:
    plain = _one(_events(_parse([_tool_result_line(None)])[0]),
                 "tool_execution_completed")
    annotated = _one(_events(_parse([_tool_result_line({"k": "v"})])[0]),
                     "tool_execution_completed")
    assert plain.dedup_key == annotated.dedup_key


# ── field-level drift ledger ─────────────────────────────────────────────────


def test_normal_lines_produce_no_field_drift_warnings() -> None:
    msgs = _parse([
        _user_line({"permissionMode": "plan", "todos": [], "isMeta": False}),
        _assistant_line({"effort": "medium", "requestId": "r1", "slug": "x"}),
    ])
    ctx = validate_messages(msgs, "s1", "claude-code", batch_safe=True)
    drift = [w for w in ctx.warnings if "field" in w.lower()]
    assert drift == []


def test_subagent_provenance_fields_produce_no_field_drift_warnings() -> None:
    """``agentId`` / ``attributionAgent`` ride every line of a subagent transcript.
    They are thread-level provenance the importer stamps onto source_metadata, so
    the ledger knows them and they must not warn — otherwise a real drift signal
    drowns in one finding per subagent run."""
    msgs = _parse([
        _user_line({"agentId": "a1b2c3"}),
        _assistant_line({"agentId": "a1b2c3", "attributionAgent": "Explore"}),
    ])
    ctx = validate_messages(msgs, "s1", "claude-code", batch_safe=True)
    drift = [w for w in ctx.warnings if "field" in w.lower()]
    assert drift == []


def test_turn_ending_tool_result_flag_produces_no_field_drift_warnings() -> None:
    """``toolEndsTurn`` rides the StructuredOutput result that closed a
    schema-constrained subagent's turn — every workflow agent given a schema
    contributes one, so an unledgered field here buries real drift."""
    msgs = _parse([_user_line({"toolEndsTurn": True}, content=[
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": "Structured output provided successfully"},
    ])])
    ctx = validate_messages(msgs, "s1", "claude-code", batch_safe=True)
    drift = [w for w in ctx.warnings if "field" in w.lower()]
    assert drift == []


def test_rejected_structured_output_stays_distinguishable_without_the_flag() -> None:
    """Why ``toolEndsTurn`` is carried but not stored: the accepted/rejected
    split it encodes is already the ``tool_execution_completed`` /
    ``tool_execution_error`` split. Claude Code omits the flag exactly when the
    schema rejects the output and the turn continues, so persisting it would
    restate this seam. If this distinction ever stops surviving, the ledger
    comment is wrong and the field has to start being kept."""
    def _result(is_error: bool) -> list:
        (msg,) = _parse([_user_line(content=[
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "Output does not match required schema" if is_error
             else "Structured output provided successfully",
             "is_error": is_error},
        ])])
        return _events(msg)

    assert _one(_result(False), "tool_execution_completed")
    assert _one(_result(True), "tool_execution_error")


def test_invented_line_field_warns() -> None:
    msgs = _parse([_user_line({"someBrandNewField": 1})])
    ctx = validate_messages(msgs, "s1", "claude-code", batch_safe=True)
    assert any("someBrandNewField" in w for w in ctx.warnings)


def test_invented_message_field_warns() -> None:
    msgs = _parse([_assistant_line(message_extras={"brandNewMessageKey": True})])
    ctx = validate_messages(msgs, "s1", "claude-code", batch_safe=True)
    assert any("brandNewMessageKey" in w for w in ctx.warnings)
