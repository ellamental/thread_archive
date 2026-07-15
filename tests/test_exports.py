"""Bulk export import: claude.ai, ChatGPT, and xAI exports each import their
conversations as threads under the right source — and classification tells the
two ``conversations.json`` providers (claude.ai vs ChatGPT) apart."""

from __future__ import annotations

import json
import zipfile

from sqlalchemy import select

from thread_archive._importers.exports import (
    classify_export,
    import_chatgpt_export,
    import_claude_ai_export,
    import_xai_export,
)
from thread_archive._store import Event, Thread, get_session, init_db


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


def test_claude_ai_export_force_reimports_into_existing_thread(archive_home) -> None:
    """``force=True`` on an already-imported conversation must reuse the existing
    thread (the thread name is unique — a second create_thread raises and used to
    leave a junk "(import error)" stub with nothing re-imported) and let dedup
    collapse the repeats, so only genuinely-new events land."""
    init_db()
    export_dir = archive_home / "claude_export"
    export_dir.mkdir()
    conv = {
        "uuid": "conv-f", "name": "Forced Chat",
        "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:00:10Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "first question",
             "content": [{"type": "text", "text": "first question"}],
             "created_at": "2026-01-01T10:00:00Z"},
        ],
    }
    path = export_dir / "conversations.json"
    path.write_text(json.dumps([conv]), encoding="utf-8")
    assert import_claude_ai_export(export_dir).imported == 1

    # The source grew a message; a forced re-import must land it in the SAME thread.
    conv["chat_messages"].append(
        {"uuid": "m2", "sender": "human", "text": "second question",
         "content": [{"type": "text", "text": "second question"}],
         "created_at": "2026-01-01T10:00:05Z"},
    )
    path.write_text(json.dumps([conv]), encoding="utf-8")
    again = import_claude_ai_export(export_dir, force=True)
    assert again.imported == 1 and again.errored == 0

    with get_session() as s:
        threads = s.execute(select(Thread).where(Thread.source == "claude")).scalars().all()
        assert [t.source_id for t in threads] == ["conv-f"], "force re-import left a stub thread"
        contents = [e.payload.get("content") for e in s.execute(
            select(Event).where(Event.thread_id == threads[0].id)
        ).scalars()]
    assert contents.count("first question") == 1, "force re-import duplicated existing events"
    assert "second question" in contents

    # Forcing an unchanged conversation collapses entirely: nothing new, no stub.
    third = import_claude_ai_export(export_dir, force=True)
    assert third.imported == 0 and third.skipped == 1 and third.errored == 0


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


# A minimal but valid ChatGPT conversation (export ``mapping`` shape).
_CHATGPT_CONV = {
    "id": "gconv-1",
    "title": "GPT Chat",
    "create_time": 1767261600.0,
    "update_time": 1767261610.0,
    "current_node": "n2",
    "mapping": {
        "root": {"id": "root", "parent": None, "children": ["n1"], "message": None},
        "n1": {"id": "n1", "parent": "root", "children": ["n2"], "message": {
            "id": "n1", "author": {"role": "user"}, "create_time": 1767261600.0,
            "content": {"content_type": "text", "parts": ["hello from chatgpt"]},
            "status": "finished_successfully", "metadata": {},
        }},
        "n2": {"id": "n2", "parent": "n1", "children": [], "message": {
            "id": "n2", "author": {"role": "assistant"}, "create_time": 1767261605.0,
            "content": {"content_type": "text", "parts": ["hi from gpt"]},
            "status": "finished_successfully", "metadata": {"model_slug": "gpt-4o"},
        }},
    },
}


def test_chatgpt_export_import(archive_home) -> None:
    init_db()
    export_dir = archive_home / "chatgpt_export"
    export_dir.mkdir()
    (export_dir / "conversations.json").write_text(json.dumps([_CHATGPT_CONV]), encoding="utf-8")
    (export_dir / "user.json").write_text(json.dumps({"id": "user-1"}), encoding="utf-8")

    assert classify_export(export_dir) == "chatgpt"
    result = import_chatgpt_export(export_dir)
    assert result.imported == 1
    assert result.events_created > 0

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "chatgpt")).scalar_one()
        assert t.title == "GPT Chat"
        assert t.source_id == "gconv-1"
        contents = [e.payload.get("content") for e in s.execute(
            select(Event).where(Event.thread_id == t.id, Event.event_type == "user_message_sent")
        ).scalars()]
    assert "hello from chatgpt" in contents

    # Re-import without force skips the existing conversation.
    again = import_chatgpt_export(export_dir)
    assert again.imported == 0 and again.skipped == 1


def test_classify_tells_chatgpt_from_claude_by_siblings(tmp_path) -> None:
    """Both providers ship ``conversations.json``; sibling files disambiguate.
    (A ChatGPT export misread as claude.ai imports zero conversations.)"""
    chatgpt_zip = tmp_path / "chatgpt.zip"
    with zipfile.ZipFile(chatgpt_zip, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_CHATGPT_CONV]))
        zf.writestr("chat.html", "<html></html>")
        zf.writestr("user.json", json.dumps({"id": "user-1"}))
    assert classify_export(chatgpt_zip) == "chatgpt"

    claude_zip = tmp_path / "claude.zip"
    with zipfile.ZipFile(claude_zip, "w") as zf:
        zf.writestr("conversations.json", json.dumps([{"uuid": "c", "chat_messages": []}]))
        zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))
    assert classify_export(claude_zip) == "claude"


def test_classify_falls_back_to_conversation_shape(tmp_path) -> None:
    """No sibling markers at all: the first conversation's own keys decide."""
    bare_chatgpt = tmp_path / "bare-chatgpt.zip"
    with zipfile.ZipFile(bare_chatgpt, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_CHATGPT_CONV]))
    assert classify_export(bare_chatgpt) == "chatgpt"

    bare_claude = tmp_path / "bare-claude"
    bare_claude.mkdir()
    (bare_claude / "conversations.json").write_text(
        json.dumps([{"uuid": "c", "chat_messages": [{"uuid": "m1"}]}]), encoding="utf-8"
    )
    assert classify_export(bare_claude) == "claude"

    # Unidentifiable content (empty list) classifies as nothing — the drop
    # watcher quarantines rather than deleting.
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("conversations.json", "[]")
    assert classify_export(empty) is None


def test_chatgpt_zip_import(archive_home) -> None:
    init_db()
    path = archive_home / "chatgpt.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([_CHATGPT_CONV]))
        zf.writestr("chat.html", "<html></html>")
    result = import_chatgpt_export(path)
    assert result.imported == 1
