"""The dedup_key backfill must recompute EXACTLY the key the builder wrote.

Idempotence of a future re-import depends on it: a backfilled key has to equal what
`build_events` produces, or the re-import won't recognize the event and will dupe it.
This drives a real thread through import, nulls its keys, and asserts recompute
restores the builder's keys for the whitelisted turn types (and leaves the
synthetic-id types NULL).
"""

from __future__ import annotations

import json

from sqlalchemy import select, update

from thread_archive._importers import import_session_incremental
from thread_archive._scripts.backfill_recompute import (
    _RECOMPUTE_SAFE,
    plan_thread,
)
from thread_archive._scripts.backfill_reconcile import _norm_key
from thread_archive._store import Event, get_session, init_db

LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "hello there"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m", "content": [
         {"type": "thinking", "thinking": "hmm"},
         {"type": "text", "text": "hi back"},
         {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-01-01T10:00:06Z",
     "sessionId": "s1", "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu1", "content": "file.txt"}]}},
    {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": "2026-01-01T10:00:08Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m",
                                    "content": [{"type": "text", "text": "done"}]}},
]


def test_recompute_reproduces_builder_keys(archive_home) -> None:
    init_db()
    f = archive_home / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, "proj:s1").thread_id

    # Snapshot the builder's keys + types (plain dicts — session closes below), then
    # null every key (simulate a pre-dedup_key thread).
    with get_session() as s:
        evs = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()
        original = {e.id: e.dedup_key for e in evs}
        by_type = {e.id: e.event_type for e in evs}
        for e in evs:
            s.execute(update(Event).where(Event.id == e.id).values(dedup_key=None))
        s.commit()

    with get_session() as s:
        backfills, _collapses, warnings, _stats = plan_thread(s, tid)

    assert not warnings
    by_id = dict(backfills)
    # every recovered key is EXACTLY the builder's (normalized) key, and only
    # whitelisted types are ever touched
    assert backfills
    for eid, key in backfills:
        assert by_type[eid] in _RECOMPUTE_SAFE
        assert key == _norm_key(original[eid], tid), f"wrong key for {by_type[eid]}"
    # an assistant turn (which carries its own api_request_started anchor) is recovered.
    assert any(by_type[eid] == "api_request_started" for eid in by_id)
    # a tool-result-only user turn has no anchor in its group, so its
    # tool_execution_completed is correctly left NULL (pmid unrecoverable).
    tool_res = [eid for eid, t in by_type.items() if t == "tool_execution_completed"]
    assert tool_res and all(eid not in by_id for eid in tool_res)


def test_recompute_is_idempotent(archive_home) -> None:
    """Once keys are present, a re-plan proposes nothing."""
    init_db()
    f = archive_home / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, "proj:s1").thread_id
    with get_session() as s:
        backfills, _c, _w, _st = plan_thread(s, tid)
    # freshly imported thread already has keys → nothing to recompute
    assert backfills == []


def test_denamespace_strips_legacy_prefix(archive_home) -> None:
    """A ``{thread_id}:``-prefixed key is stripped back to the current bare form."""
    from thread_archive._scripts.denamespace_dedup_keys import plan_thread as deprefix_plan

    init_db()
    f = archive_home / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, "proj:s1").thread_id

    # Simulate the legacy format: prepend {thread_id}: to every key.
    with get_session() as s:
        evs = s.execute(select(Event).where(Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().all()
        bare = {e.id: e.dedup_key for e in evs}
        for e in evs:
            s.execute(update(Event).where(Event.id == e.id).values(dedup_key=f"{tid}:{e.dedup_key}"))
        s.commit()

    with get_session() as s:
        updates, stats = deprefix_plan(s, tid)
    assert stats["stripped"] == len(bare)
    assert stats.get("collapses_onto_existing", 0) == 0
    for eid, old, new in updates:
        assert old == f"{tid}:{bare[eid]}"
        assert new == bare[eid]   # back to exactly the current bare key


def test_collapse_deletes_keyed_reimport_beside_null_key_original(archive_home) -> None:
    """The lost-watermark shape: a pre-dedup-era thread (NULL keys) gets re-imported,
    landing every turn again as keyed rows. ``--collapse`` must key the originals,
    delete the re-imported copies, and leave the thread single-copy + fully keyed."""
    from thread_archive._scripts.backfill_recompute import run as recompute_run

    init_db()
    f = archive_home / "s.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, "proj:s1").thread_id

    # Make the imported rows pre-dedup-era, then rewind the watermark and
    # re-import — the membership check can't match NULL keys, so every turn dupes.
    from sqlalchemy import text as sa_text
    with get_session() as s:
        s.execute(update(Event).where(Event.thread_id == tid).values(dedup_key=None))
        s.execute(sa_text("UPDATE import_state SET last_line_count = 0, last_file_size = 0"))
        s.commit()
    import_session_incremental(f, "proj:s1")

    with get_session() as s:
        n_duped = len(s.execute(select(Event).where(Event.thread_id == tid)).scalars().all())

    totals = recompute_run(apply=True, collapse=True)
    assert totals["collapses"] > 0

    with get_session() as s:
        evs = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()
        # Every duplicated pair collapsed back to one copy...
        assert len(evs) == n_duped - totals["collapses"]
        # ...the survivors are the ORIGINAL (lower-id) rows, now keyed.
        recompute_safe = [e for e in evs if e.event_type in _RECOMPUTE_SAFE]
        assert all(e.dedup_key for e in recompute_safe)
        dup_pairs = s.execute(sa_text(
            "SELECT count(*) FROM (SELECT 1 FROM events WHERE dedup_key IS NOT NULL "
            "GROUP BY thread_id, dedup_key HAVING count(*) > 1)"
        )).scalar()
        assert dup_pairs == 0
        # FTS shadow rows of the deleted copies are gone too.
        live_ids = {e.id for e in evs}
        fts_ids = {r[0] for r in s.execute(sa_text(
            "SELECT event_id FROM events_fts WHERE thread_id = :t"), {"t": tid})}
        assert fts_ids <= live_ids
