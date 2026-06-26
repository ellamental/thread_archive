"""Continuation / fork detection: a post-compaction CC session merges into its
parent thread (instead of fragmenting), a fork stays its own thread, and a merged
replay never duplicates the shared prefix.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select

from thread_archive.importers import import_session_incremental
from thread_archive.store import Event, Thread, get_session, init_db

PARENT_UUID = "11111111-1111-1111-1111-111111111111"

PU = {"type": "user", "uuid": "pu1", "timestamp": "2026-01-01T10:00:00Z",
      "sessionId": PARENT_UUID, "cwd": "/proj",
      "message": {"role": "user", "content": "original question"}}
PA = {"type": "assistant", "uuid": "pa1", "timestamp": "2026-01-01T10:00:05Z",
      "sessionId": PARENT_UUID,
      "message": {"role": "assistant", "model": "claude-opus-4",
                  "content": [{"type": "text", "text": "original answer"}]}}
PU2 = {"type": "user", "uuid": "pu2", "timestamp": "2026-01-01T10:01:00Z",
       "sessionId": PARENT_UUID, "cwd": "/proj",
       "message": {"role": "user", "content": "second question"}}
PA2 = {"type": "assistant", "uuid": "pa2", "timestamp": "2026-01-01T10:01:05Z",
       "sessionId": PARENT_UUID,
       "message": {"role": "assistant", "model": "claude-opus-4",
                   "content": [{"type": "text", "text": "second answer"}]}}


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _thread_count() -> int:
    with get_session() as s:
        return len(s.execute(select(Thread).where(Thread.source == "claude-code")).scalars().all())


def _user_msg_contents() -> list[str]:
    with get_session() as s:
        return s.execute(
            select(func.json_extract(Event.payload, "$.content"))
            .where(Event.event_type == "user_message_sent")
            .order_by(Event.id)
        ).scalars().all()


def test_compact_boundary_continuation_merges_into_parent(archive_home) -> None:
    init_db()
    parent = archive_home / "parent.jsonl"
    _write_jsonl(parent, [PU, PA])
    import_session_incremental(parent, f"proj:{PARENT_UUID}")
    assert _thread_count() == 1

    cont = archive_home / "cont.jsonl"
    _write_jsonl(cont, [
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-01T11:00:00Z"},
        {"type": "user", "uuid": "cu1", "timestamp": "2026-01-01T11:00:01Z",
         "sessionId": "22222222-2222-2222-2222-222222222222", "cwd": "/proj",
         "message": {"role": "user",
                     "content": f"This session is being continued from /Users/ella/.claude/projects/proj/{PARENT_UUID}.jsonl\n\nfollow up question"}},
        {"type": "assistant", "uuid": "ca1", "timestamp": "2026-01-01T11:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "follow up answer"}]}},
    ])
    result = import_session_incremental(cont, "proj:22222222-2222-2222-2222-222222222222")

    # Merged: still one thread, and it carries the follow-up turn.
    assert result.is_new_thread is False
    assert _thread_count() == 1
    contents = _user_msg_contents()
    assert "original question" in contents
    assert any("follow up question" in (c or "") for c in contents)


def test_no_boundary_replay_merges_without_duplicating_prefix(archive_home) -> None:
    init_db()
    parent = archive_home / "parent.jsonl"
    _write_jsonl(parent, [PU, PA])
    import_session_incremental(parent, f"proj:{PARENT_UUID}")

    # An older-style compaction: a new file replaying the prefix (fresh UUIDs, same
    # content + timestamps) then a new tail.
    replay = archive_home / "replay.jsonl"
    _write_jsonl(replay, [
        {**PU, "uuid": "ru1"},
        {**PA, "uuid": "ra1"},
        {"type": "user", "uuid": "ru2", "timestamp": "2026-01-01T10:05:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": "a brand new question"}},
        {"type": "assistant", "uuid": "ra2", "timestamp": "2026-01-01T10:05:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "a brand new answer"}]}},
    ])
    import_session_incremental(replay, "proj:33333333-3333-3333-3333-333333333333")

    assert _thread_count() == 1
    contents = _user_msg_contents()
    # Prefix not duplicated, new tail present.
    assert contents.count("original question") == 1
    assert "a brand new question" in contents


def test_fork_does_not_merge(archive_home) -> None:
    init_db()
    parent = archive_home / "parent.jsonl"
    _write_jsonl(parent, [PU, PA, PU2, PA2])
    import_session_incremental(parent, f"proj:{PARENT_UUID}")
    assert _thread_count() == 1

    # A fork: shares the first turn, then diverges at the second user message.
    fork = archive_home / "fork.jsonl"
    _write_jsonl(fork, [
        {**PU, "uuid": "fu1"},
        {**PA, "uuid": "fa1"},
        {"type": "user", "uuid": "fu2", "timestamp": "2026-01-01T10:01:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": "a DIVERGENT second question"}},
        {"type": "assistant", "uuid": "fa2", "timestamp": "2026-01-01T10:01:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "divergent answer"}]}},
    ])
    result = import_session_incremental(fork, "proj:44444444-4444-4444-4444-444444444444")

    # Diverged → its own thread, not merged into the parent.
    assert result.is_new_thread is True
    assert _thread_count() == 2


def test_reimport_merged_continuation_is_idempotent(archive_home) -> None:
    init_db()
    parent = archive_home / "parent.jsonl"
    _write_jsonl(parent, [PU, PA])
    import_session_incremental(parent, f"proj:{PARENT_UUID}")

    replay = archive_home / "replay.jsonl"
    _write_jsonl(replay, [
        {**PU, "uuid": "ru1"}, {**PA, "uuid": "ra1"},
        {"type": "user", "uuid": "ru2", "timestamp": "2026-01-01T10:05:00Z", "cwd": "/proj",
         "message": {"role": "user", "content": "tail question"}},
    ])
    import_session_incremental(replay, "proj:33333333-3333-3333-3333-333333333333")
    contents_before = _user_msg_contents()

    # Re-import the same continuation file: nothing new lands.
    result = import_session_incremental(replay, "proj:33333333-3333-3333-3333-333333333333")
    assert result.events_created == 0
    assert _user_msg_contents() == contents_before
