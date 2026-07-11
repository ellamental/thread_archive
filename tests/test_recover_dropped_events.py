"""The recovery backfill: re-parse a thread's source and restore the events the
old builder dropped (ide_context / content_block / message), joining them onto the
existing turns' streams, without touching or duplicating anything already imported.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval.fts import index_events
from thread_archive._scripts.recover_dropped_events import RECOVERABLE_TYPES, plan_thread
from thread_archive._store import Event, get_session, init_db
from thread_archive._truth import write_events


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/proj", "message": {"role": "user", "content": "hello"}},
    {"type": "user", "uuid": "u_ide", "timestamp": "2026-01-01T10:00:01Z", "sessionId": "s1",
     "message": {"role": "user", "content": "<ide_selection>def foo(): pass</ide_selection>"}},
    {"type": "user", "uuid": "u_mix", "timestamp": "2026-01-01T10:00:02Z", "sessionId": "s1",
     "message": {"role": "user", "content": "look\n<ide_selection>def bar(): pass</ide_selection>"}},
    {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z", "sessionId": "s1",
     "message": {"role": "assistant", "model": "m", "content": [
         {"type": "text", "text": "ok"},
         {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "z"}}]}},
]


def test_backfill_recovers_dropped_events_idempotently(archive_home) -> None:
    init_db()
    f = archive_home / "sess.jsonl"
    _write_jsonl(f, LINES)
    tid = import_session_incremental(f, "proj:s1").thread_id

    # Streams of the surviving (non-recoverable) turns, to prove re-association.
    with get_session() as s:
        pre = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()
    stream_by_anchor = {
        e.dedup_key.split(":", 1)[0]: e.stream_id
        for e in pre if e.event_type not in RECOVERABLE_TYPES and e.dedup_key
    }
    n_pre = len(pre)

    # Simulate a pre-fix archive: every recoverable-type event is gone.
    with get_session() as s:
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type.in_(list(RECOVERABLE_TYPES))
        ))
        s.commit()

    # Plan restores exactly the dropped events.
    with get_session() as s:
        rows = plan_thread(s, tid, LINES)
    by_anchor: dict[str, list] = {}
    for r in rows:
        by_anchor.setdefault(r.dedup_key.split(":", 1)[0], []).append(r)

    assert {r.event_type for r in rows} == {"ide_context", "content_block"}
    # Fully-dropped ide-only turn is back; mixed turn and assistant turn too.
    assert {"u_ide", "u_mix", "a1"} <= set(by_anchor)
    # Re-association: recovered events reuse the surviving turn's stream.
    assert by_anchor["u_mix"][0].stream_id == stream_by_anchor["u_mix"]
    assert by_anchor["a1"][0].stream_id == stream_by_anchor["a1"]

    # Apply, then confirm we're whole again and a second run is a no-op.
    with get_session() as s:
        rows = plan_thread(s, tid, LINES)
        write_events(s, rows)
        index_events(s, rows)
        s.commit()
    with get_session() as s:
        assert plan_thread(s, tid, LINES) == []          # idempotent
        assert len(s.execute(                            # back to the full set, no dupes
            select(Event).where(Event.thread_id == tid)
        ).scalars().all()) == n_pre
