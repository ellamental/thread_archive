"""Claude Code importer: import a fixture transcript, prove repeated
import is idempotent (both by watermark and by dedup-key), and that the watermark
advances only on new content.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select

from thread_archive.importers import import_session_incremental
from thread_archive.store import Event, ImportState, Thread, _base, get_session, init_db
from thread_archive.truth import checkpoint, jsonl_log, reindex

USER = {
    "type": "user",
    "uuid": "u1",
    "timestamp": "2026-01-01T10:00:00Z",
    "sessionId": "s1",
    "cwd": "/proj",
    "message": {"role": "user", "content": "hello world"},
}
ASSISTANT = {
    "type": "assistant",
    "uuid": "a1",
    "timestamp": "2026-01-01T10:00:05Z",
    "sessionId": "s1",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4",
        "content": [{"type": "text", "text": "hi there"}],
    },
}


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _event_count(thread_id=None) -> int:
    with get_session() as s:
        stmt = select(Event)
        if thread_id is not None:
            stmt = stmt.where(Event.thread_id == thread_id)
        return len(s.execute(stmt).scalars().all())


def test_import_creates_thread_and_events(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    result = import_session_incremental(f, "proj:s1")
    assert result.is_new_thread is True
    assert result.events_created > 0
    assert result.last_message_uuid == "a1"

    with get_session() as s:
        thread = s.execute(select(Thread).where(Thread.source == "claude-code")).scalar_one()
        tid = thread.id
        assert thread.source_id == "proj:s1"
        assert thread.source_metadata == {"cwd": "/proj", "project_dir": "proj"}
        events = s.execute(select(Event).where(Event.thread_id == thread.id)).scalars().all()

    user_events = [e for e in events if e.event_type == "user_message_sent"]
    assert any(e.payload.get("content") == "hello world" for e in user_events)

    # Truth tie-in: the imported events are in the thread's per-thread truth file.
    tf = next((archive_home / "truth" / "threads").rglob(f"{tid}.jsonl"))
    recs = [json.loads(ln) for ln in tf.read_text().splitlines() if ln.strip()]
    assert sum(1 for r in recs if r["type"] == "event") == len(events)
    assert any(r["type"] == "thread" for r in recs)  # metadata record present


def test_reimport_unchanged_file_is_noop(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    import_session_incremental(f, "proj:s1")
    n1 = _event_count()
    result = import_session_incremental(f, "proj:s1")
    assert result.events_created == 0  # file-size watermark short-circuits
    assert _event_count() == n1


def test_dedup_key_idempotent_even_without_watermark(archive_home) -> None:
    """The real idempotence guarantee: wipe the watermark, re-import the same file,
    and the dedup-key membership check still adds nothing."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])

    import_session_incremental(f, "proj:s1")
    n1 = _event_count()

    with get_session() as s:
        s.execute(delete(ImportState))
        s.commit()

    result = import_session_incremental(f, "proj:s1")
    assert result.events_created == 0
    assert result.is_new_thread is False  # resolved the existing thread, didn't recreate
    assert _event_count() == n1


def test_watermark_advances_on_appended_turns(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    n1 = _event_count()

    user2 = {**USER, "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
             "message": {"role": "user", "content": "second question"}}
    assistant2 = {**ASSISTANT, "uuid": "a2", "timestamp": "2026-01-01T10:01:05Z",
                  "message": {"role": "assistant", "model": "claude-opus-4",
                              "content": [{"type": "text", "text": "second answer"}]}}
    _write_jsonl(f, [USER, ASSISTANT, user2, assistant2])

    result = import_session_incremental(f, "proj:s1")
    assert result.events_created > 0
    assert _event_count() > n1

    with get_session() as s:
        events = s.execute(select(Event)).scalars().all()
        state = s.execute(select(ImportState)).scalar_one()
    assert any(
        e.payload.get("content") == "second question"
        for e in events
        if e.event_type == "user_message_sent"
    )
    assert state.last_line_count == 4  # watermark advanced to the full file
    assert state.last_message_uuid == "a2"


def test_import_then_checkpoint_reindex_is_lossless(archive_home) -> None:
    """Import → checkpoint → delete index.db → reindex must
    reproduce the thread *and* its events (the importer streams events to truth;
    checkpoint snapshots the thread)."""
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, [USER, ASSISTANT])
    import_session_incremental(f, "proj:s1")
    checkpoint()

    with get_session() as s:
        before_events = [
            (e.event_type, e.payload.get("content")) for e in
            s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        before_thread = s.execute(select(Thread)).scalar_one()
        before_title = before_thread.title

    # Nuke the index; truth is the only surviving copy.
    from thread_archive.store import get_engine

    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    counts = reindex()
    assert counts["threads"] == 1
    assert counts["events"] == len(before_events)

    with get_session() as s:
        after_events = [
            (e.event_type, e.payload.get("content")) for e in
            s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        after_thread = s.execute(select(Thread)).scalar_one()
    assert after_events == before_events
    assert after_thread.source_id == "proj:s1"
    assert after_thread.title == before_title
