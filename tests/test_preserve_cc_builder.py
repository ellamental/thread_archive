"""Preservation contract: the ingest path must not silently drop content.

These pin the fixes that close known drop-holes:

* builder — an image-only user turn (screenshot paste, no caption) is kept, and
  an unmodeled user content block becomes its own ``content_block`` event;
* CC parser — an unrecognized line ``type``, a non-``queued_command`` attachment,
  and an empty-``content`` system line that still carries metadata are all
  preserved as records rather than returned as ``None``.
"""

from __future__ import annotations

from thread_import import DefaultEventBuilder
from thread_import.parsers.claude_code import ClaudeCodeParser


def _build(msg: dict) -> list:
    return DefaultEventBuilder().build_events(msg, "stream-1")


# ── builder: user-turn preservation ─────────────────────────────────────────
def test_image_only_user_turn_is_kept() -> None:
    """A user turn with images but no text must still emit user_message_sent
    (carrying the images) — not be dropped as 'empty'."""
    msg = {
        "role": "user",
        "provider_message_id": "u_img",
        "created_at": "2026-01-01T10:00:00Z",
        "content_text": "",
        "content_blocks": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
        ],
    }
    events = _build(msg)
    kinds = [e.event_type for e in events]
    assert "user_message_sent" in kinds
    ev = next(e for e in events if e.event_type == "user_message_sent")
    assert ev.payload.get("content_type") == "multimodal"
    assert ev.payload["images"] and ev.payload["images"][0]["data"] == "aGk="


def test_unknown_user_block_becomes_content_block_event() -> None:
    """An unmodeled block type on a user turn is preserved verbatim as a
    content_block event (mirrors the assistant path), not dropped."""
    msg = {
        "role": "user",
        "provider_message_id": "u_x",
        "created_at": "2026-01-01T10:00:00Z",
        "content_text": "hi",
        "content_blocks": [
            {"type": "text", "text": "hi", "seq": 0},
            {"type": "mcp_tool_use", "name": "weird", "payload": {"k": 1}, "seq": 1},
        ],
    }
    events = _build(msg)
    cb = [e for e in events if e.event_type == "content_block"]
    assert len(cb) == 1
    assert cb[0].payload["block_type"] == "mcp_tool_use"
    assert cb[0].payload["data"]["payload"] == {"k": 1}


# ── CC parser: line-level preservation ──────────────────────────────────────
def _parse(lines: list[dict]) -> list[dict]:
    parser = ClaudeCodeParser()
    return parser.parse_export(
        {"provider": "claude-code", "sessions": [{"session_id": "s1", "project": "/p", "lines": lines}]}
    )


def _has_event(msg: dict) -> bool:
    return bool(DefaultEventBuilder().build_events(msg, "s"))


def test_unknown_cc_line_type_preserved() -> None:
    """A line whose `type` the parser doesn't model is kept as a record carrying
    the raw line — nothing vanishes, not even a future line kind."""
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
         "message": {"role": "user", "content": "hi"}},
        {"type": "telemetry_v2", "uuid": "t1", "timestamp": "2026-01-01T10:00:01Z", "sessionId": "s1",
         "payload": {"cpu": 0.9, "note": "brand new line kind"}},
    ]
    msgs = _parse(lines)
    preserved = [m for m in msgs if any(
        isinstance(b, dict) and b.get("type") == "unknown_line" for b in m.get("content_blocks", [])
    )]
    assert len(preserved) == 1
    raw = preserved[0]["content_blocks"][0]["raw"]
    assert raw["payload"]["note"] == "brand new line kind"
    assert _has_event(preserved[0])


def test_non_queued_attachment_preserved() -> None:
    """A non-queued_command attachment (todo reminder, tool-listing delta) is
    preserved as a hidden system record, not dropped."""
    lines = [
        {"type": "attachment", "uuid": "at1", "timestamp": "2026-01-01T10:00:02Z", "sessionId": "s1",
         "attachment": {"type": "todo_reminder", "content": "remember X"}},
    ]
    msgs = _parse(lines)
    preserved = [m for m in msgs if any(
        isinstance(b, dict) and b.get("type") == "attachment" for b in m.get("content_blocks", [])
    )]
    assert len(preserved) == 1
    assert preserved[0]["content_blocks"][0]["raw"]["content"] == "remember X"
    assert _has_event(preserved[0])


def test_empty_content_system_line_with_metadata_preserved() -> None:
    """A system line with no `content` string but real metadata (a
    compact_boundary marker) is kept, not dropped."""
    lines = [
        {"type": "system", "uuid": "sy1", "timestamp": "2026-01-01T10:00:03Z", "sessionId": "s1",
         "content": "", "subtype": "compact_boundary",
         "compactMetadata": {"preTokens": 12345, "trigger": "auto"}},
    ]
    msgs = _parse(lines)
    sysmsgs = [m for m in msgs if m.get("role") == "system"]
    assert len(sysmsgs) == 1
    assert sysmsgs[0]["provider_data"]["line"]["compactMetadata"]["preTokens"] == 12345
    assert _has_event(sysmsgs[0])
