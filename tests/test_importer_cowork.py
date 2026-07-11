"""Cowork import: a Claude-Code-shaped ``audit.jsonl`` imports under
``source='cowork'`` with the title from its metadata file, the ``_audit_timestamp``
backfilled, and the ``isReplay`` echo neutered (no duplicate user turn)."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from thread_archive._importers import import_cowork_session_incremental
from thread_archive._store import Event, Thread, get_session, init_db


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_cowork_import_titles_and_neuters_replay(archive_home) -> None:
    init_db()
    session_dir = archive_home / "local_sess1"
    session_dir.mkdir()
    audit = session_dir / "audit.jsonl"
    _write_jsonl(audit, [
        {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "s1", "cwd": "/proj",
         "message": {"role": "user", "content": "cowork task"}},
        # The SDK echo of the same submission — must not create a second user turn.
        {"type": "user", "uuid": "u1", "isReplay": True, "timestamp": "2026-01-01T10:00:02Z",
         "message": {"role": "user", "content": "cowork task"}},
        {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "working on it"}]}},
    ])
    meta = archive_home / "local_sess1.json"
    meta.write_text(json.dumps({"title": "My Cowork Task"}), encoding="utf-8")

    result = import_cowork_session_incremental(audit, "user:org:sess1", meta)
    assert result.is_new_thread is True
    assert result.events_created > 0

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "cowork")).scalar_one()
        assert t.title == "My Cowork Task"
        assert t.source_id == "user:org:sess1"
        user_contents = s.execute(
            select(func.json_extract(Event.payload, "$.content"))
            .where(Event.thread_id == t.id, Event.event_type == "user_message_sent")
        ).scalars().all()
    # The replay echo was neutered → exactly one user turn.
    assert user_contents.count("cowork task") == 1


def test_cowork_reimport_is_noop(archive_home) -> None:
    init_db()
    session_dir = archive_home / "local_sess2"
    session_dir.mkdir()
    audit = session_dir / "audit.jsonl"
    _write_jsonl(audit, [
        {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "task two"}},
        {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "ok"}]}},
    ])
    import_cowork_session_incremental(audit, "user:org:sess2", None)
    again = import_cowork_session_incremental(audit, "user:org:sess2", None)
    assert again.events_created == 0
    assert again.is_new_thread is False
