"""Bulk export import: a claude.ai batch-directory export and an xAI export
directory each import their conversations as threads under the right source."""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive.importers.exports import (
    classify_export,
    import_claude_ai_export,
    import_xai_export,
)
from thread_archive.store import Event, Thread, get_session, init_db


def test_claude_ai_export_import(archive_home) -> None:
    init_db()
    export_dir = archive_home / "claude_export"
    export_dir.mkdir()
    conv = {
        "uuid": "conv-1", "name": "My Web Chat",
        "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:00:10Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "hello from web",
             "content": [{"type": "text", "text": "hello from web"}],
             "created_at": "2026-01-01T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "hi from claude",
             "content": [{"type": "text", "text": "hi from claude"}],
             "created_at": "2026-01-01T10:00:05Z"},
        ],
    }
    (export_dir / "conversations.json").write_text(json.dumps([conv]), encoding="utf-8")
    (export_dir / "users.json").write_text(json.dumps([{"uuid": "user-1"}]), encoding="utf-8")

    assert classify_export(export_dir) == "claude"
    result = import_claude_ai_export(export_dir)
    assert result.imported == 1
    assert result.events_created > 0

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude")).scalar_one()
        assert t.title == "My Web Chat"
        assert t.source_id == "conv-1"
        contents = [e.payload.get("content") for e in s.execute(
            select(Event).where(Event.thread_id == t.id, Event.event_type == "user_message_sent")
        ).scalars()]
    assert "hello from web" in contents

    # Re-import without force skips the existing conversation.
    again = import_claude_ai_export(export_dir)
    assert again.imported == 0 and again.skipped == 1


def test_xai_export_import(archive_home) -> None:
    init_db()
    export_dir = archive_home / "xai_export" / "ttl" / "30d" / "export_data" / "user-uuid"
    export_dir.mkdir(parents=True)
    payload = {
        "conversations": [
            {
                "conversation": {"id": "xconv-1", "title": "Grok Chat",
                                 "create_time": "2026-01-01T10:00:00Z"},
                "responses": [
                    {"response": {"_id": "r1", "sender": "human", "message": "grok question",
                                  "create_time": "2026-01-01T10:00:00Z"}},
                    {"response": {"_id": "r2", "sender": "ASSISTANT", "message": "grok answer",
                                  "model": "grok-4", "create_time": "2026-01-01T10:00:05Z"}},
                ],
            }
        ]
    }
    (export_dir / "prod-grok-backend.json").write_text(json.dumps(payload), encoding="utf-8")

    root = archive_home / "xai_export"
    assert classify_export(root) == "xai"
    result = import_xai_export(root)
    assert result.imported == 1

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "grok")).scalar_one()
        assert t.title == "Grok Chat"
        assert t.source_id == "xconv-1"
        assert (t.source_metadata or {}).get("models") == ["grok-4"]
