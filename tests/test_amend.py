"""Amendment = append-only edit: a superseding truth line, never a rewrite.

The mechanism's contract: an amended event keeps its id, dedup_key, and history
(the pre-amendment line stays in the truth file), the index and truth agree
after every amend, a reindex converges to the amended value (last-wins), and
the content-hash gates stay green because amendments cannot touch content
fields. The usage/cost backfill script rides the same mechanism and must be
idempotent."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, update

from thread_archive._importers import import_session_incremental
from thread_archive._ops.amend import amend_event_payloads, load_amendments
from thread_archive._scripts.backfill_usage_cost import run as backfill_run
from thread_archive._store import Event, get_session, init_db
from thread_archive._truth.jsonl_log import _iter_jsonl, _shard_depth, _thread_file, log_dir
from thread_archive._truth.rebuild import _store_rows_failing_key_hash, reindex

USAGE = {
    "input_tokens": 11, "output_tokens": 7, "thinking_tokens": 0,
    "cache_read_tokens": 1200, "cache_write_tokens": 300,
}

LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "hello there"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m", "usage": dict(USAGE),
                                    "cost": 0.0421,
                                    "content": [{"type": "text", "text": "hi back"}]}},
]


def _import(archive_home, source="claude-code", stem="s1"):
    init_db()
    f = archive_home / f"{stem}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, stem, source=source).thread_id
    return f, tid


def _completed_event(tid):
    with get_session() as s:
        ev = s.execute(
            select(Event).where(
                Event.thread_id == tid, Event.event_type == "api_request_completed"
            )
        ).scalar_one()
        return ev.id, dict(ev.payload), ev.dedup_key


def _truth_lines_for(tid, eid):
    path = _thread_file(log_dir(), tid, _shard_depth(log_dir()))
    return [
        r for r in _iter_jsonl(path)
        if r.get("type", "event") == "event" and r.get("id") == eid
    ]


def test_amend_appends_superseding_line_and_updates_index(archive_home) -> None:
    _, tid = _import(archive_home)
    eid, payload, key = _completed_event(tid)
    # Simulate a pre-fix import: strip the fields the old importer dropped.
    stripped = {k: v for k, v in payload.items()
                if k not in ("cost", "cache_read_tokens", "cache_write_tokens")}
    with get_session() as s:
        s.execute(update(Event).where(Event.id == eid).values(payload=stripped))
        s.commit()

    res = amend_event_payloads(
        [(tid, eid, {"cost": 0.0421, "cache_read_tokens": 1200})], reason="test"
    )
    assert res["events_amended"] == 1

    # Index carries the merged payload; identity untouched.
    with get_session() as s:
        ev = s.get(Event, eid)
        assert ev.payload["cost"] == 0.0421
        assert ev.payload["cache_read_tokens"] == 1200
        assert ev.payload["model"] == "m"
        assert ev.dedup_key == key

    # Truth: the original line persists as history; the last line is the amendment.
    lines = _truth_lines_for(tid, eid)
    assert len(lines) >= 2
    assert "cost" not in lines[-2]["payload"] or lines[-2]["payload"] != lines[-1]["payload"]
    assert lines[-1]["payload"]["cost"] == 0.0421
    assert lines[-1]["dedup_key"] == key

    # Audit record with before-values (reversible).
    recs = load_amendments()
    assert recs and recs[-1]["event_id"] == eid and recs[-1]["reason"] == "test"
    assert recs[-1]["before"] == {"cost": None, "cache_read_tokens": None}

    # The merged payload still re-hashes to its key, and a reindex converges
    # to the amended value (last-wins).
    failing, _sample = _store_rows_failing_key_hash()
    assert failing == 0
    reindex()
    with get_session() as s:
        assert s.get(Event, eid).payload["cost"] == 0.0421


def test_amend_refuses_content_fields_and_redacted(archive_home) -> None:
    _, tid = _import(archive_home)
    eid, _payload, _key = _completed_event(tid)
    with pytest.raises(ValueError, match="content-identity"):
        amend_event_payloads([(tid, eid, {"content_blocks": []})])
    with pytest.raises(ValueError, match="not in thread"):
        amend_event_payloads([(tid, eid + 999, {"cost": 1})])
    with get_session() as s:
        s.execute(update(Event).where(Event.id == eid).values(
            payload={"_redacted": {"key_id": "k", "at": "t"}}
        ))
        s.commit()
    with pytest.raises(ValueError, match="redacted"):
        amend_event_payloads([(tid, eid, {"cost": 1})])


def test_amend_noop_skips_without_truth_append(archive_home) -> None:
    _, tid = _import(archive_home)
    eid, payload, _key = _completed_event(tid)
    before = len(_truth_lines_for(tid, eid))
    res = amend_event_payloads([(tid, eid, {"cost": payload["cost"]})])
    assert res == {"events_amended": 0, "events_skipped": 1, "threads": 1}
    assert len(_truth_lines_for(tid, eid)) == before
    assert load_amendments() == []


def test_token_amendment_invalidates_stats_rollup(archive_home) -> None:
    _, tid = _import(archive_home)
    from thread_archive import _api as ta

    assert ta.stats()["overview"]["input_tokens"] == 11
    eid, _payload, _key = _completed_event(tid)
    amend_event_payloads([(tid, eid, {"input_tokens": 5})], reason="normalize")

    # The event id sits behind the rollup cursor, so this can only observe the
    # amendment when amend_event_payloads explicitly rewinds the projection.
    assert ta.stats()["overview"]["input_tokens"] == 5


def test_backfill_usage_cost_restores_dropped_fields(archive_home) -> None:
    f, tid = _import(archive_home)
    eid, payload, _key = _completed_event(tid)
    stripped = {k: v for k, v in payload.items()
                if k not in ("cost", "cache_read_tokens", "cache_write_tokens")}
    with get_session() as s:
        s.execute(update(Event).where(Event.id == eid).values(payload=stripped))
        s.commit()

    pairs = [("claude-code", f, "s1")]
    dry = backfill_run(apply=False, pairs=pairs)
    assert dry["patches"] == 1
    assert dry["field:cost"] == 1 and dry["field:cache_read_tokens"] == 1
    with get_session() as s:  # dry-run wrote nothing
        assert "cost" not in s.get(Event, eid).payload

    applied = backfill_run(apply=True, pairs=pairs)
    assert applied["events_amended"] == 1
    with get_session() as s:
        p = s.get(Event, eid).payload
        assert p["cost"] == 0.0421
        assert p["cache_read_tokens"] == 1200 and p["cache_write_tokens"] == 300

    # Idempotent: a re-run finds nothing missing.
    again = backfill_run(apply=True, pairs=pairs)
    assert again.get("patches", 0) == 0 and again.get("already_complete") == 1
