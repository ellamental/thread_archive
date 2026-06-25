"""JSONL is truth, SQLite is a lossless rebuildable projection.

- write a thread + its events through the truth seam (one file per thread)
- the per-thread JSONL file exists and holds the metadata record + event lines
- delete index.db, run reindex, and the same thread + event set comes back
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from thread_archive.store import Event, Thread, _base, get_engine, get_session, init_db
from thread_archive.truth import jsonl_log

# archive_home fixture lives in tests/conftest.py


def _now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _delete_index(home) -> None:
    for suffix in ("", "-wal", "-shm"):
        (home / f"index.db{suffix}").unlink(missing_ok=True)


def _thread_file(home, tid):
    """The thread's truth file under truth/threads/ (depth-agnostic lookup)."""
    matches = list((home / "truth" / "threads").rglob(f"{tid}.jsonl"))
    return matches[0] if matches else None


def test_jsonl_truth_and_lossless_reindex(archive_home) -> None:
    init_db()

    # Author a thread and record it to truth (the metadata-write seam) so its file
    # opens with the {"type":"thread"} record.
    with get_session() as s:
        t = Thread(name="sess-1", title="hi", source="claude-code", source_id="abc")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    # Append events through the truth seam — they hit the thread's file on commit.
    with get_session() as s:
        jsonl_log.write_events(
            s,
            [
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="user_message_sent",
                    payload={"text": "hi"},
                    occurred_at=_now(),
                    dedup_key="k1",
                ),
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="text_delta",
                    payload={"text": "yo"},
                    occurred_at=_now(),
                    dedup_key="k2",
                ),
            ],
        )
        s.commit()

    # One file per thread: threads/<id>.jsonl = the thread record + its 2 events.
    tf = _thread_file(archive_home, tid)
    assert tf is not None, "expected a per-thread truth file"
    recs = [json.loads(ln) for ln in tf.read_text().splitlines() if ln.strip()]
    kinds = [r["type"] for r in recs]
    assert kinds.count("thread") >= 1
    assert kinds.count("event") == 2

    # Capture the event set as the live store sees it.
    with get_session() as s:
        before = [
            (e.id, e.thread_id, e.event_type, e.payload["text"], e.dedup_key)
            for e in s.execute(select(Event).order_by(Event.id)).scalars()
        ]
    assert [b[3] for b in before] == ["hi", "yo"]

    # Nuke the index entirely — JSONL is the only surviving copy.
    get_engine().dispose()
    jsonl_log.reset_handles()
    _base.close_engine()
    _delete_index(archive_home)
    assert not (archive_home / "index.db").exists()

    # Rebuild from truth.
    counts = jsonl_log.reindex()
    assert counts["threads"] == 1
    assert counts["events"] == 2

    with get_session() as s:
        after = [
            (e.id, e.thread_id, e.event_type, e.payload["text"], e.dedup_key)
            for e in s.execute(select(Event).order_by(Event.id)).scalars()
        ]
        thread = s.execute(select(Thread).where(Thread.name == "sess-1")).scalar_one()

    assert after == before, "reindex from JSONL must reproduce the exact event set"
    assert thread.title == "hi"
    assert thread.source == "claude-code"
    assert thread.id == tid


def test_rollback_does_not_write_truth(archive_home) -> None:
    """A staged event whose transaction rolls back must not reach the truth."""
    init_db()
    with get_session() as s:
        t = Thread(name="sess-1")
        s.add(t)
        s.flush()
        jsonl_log.record_thread(s, t)
        s.commit()
        tid = t.id

    with get_session() as s:
        jsonl_log.write_events(
            s,
            [
                Event(
                    thread_id=tid,
                    stream_id="x",
                    event_type="user_message_sent",
                    payload={"text": "discard me"},
                    occurred_at=_now(),
                ),
            ],
        )
        s.rollback()

    # The thread's file holds its metadata record but never the rolled-back event.
    tf = _thread_file(archive_home, tid)
    assert tf is not None
    assert "discard me" not in tf.read_text()
