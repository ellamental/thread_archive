"""backfill_dropped_fields: missing-only merges, placeholder overwrites, salvage
annotations, and allowlisted inserts — all amendment-legal, all idempotent.

Fixtures seed a store through the real importers, then rewrite stored payloads
into the OLD importers' shape (dropped keys, zero-token placeholders, hollow
errors, the "cursor" model placeholder, deleted thinking rows) — including
recomputing the dedup_key where the old content genuinely differed — and assert
the script restores exactly the non-content fields, never content identity.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update

from thread_archive._importers import (
    import_antigravity_session_incremental,
    import_claude_science_db,
    import_codex_session_incremental,
    import_cowork_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from thread_archive._importers._read import read_session_lines
from thread_archive._ops.amend import load_amendments
from thread_archive._scripts import backfill_dropped_fields as bdf
from thread_archive._scripts.backfill_dropped_fields import (
    ThreadPlan,
    WorkItem,
    _apply_thread,
    _insertable,
    _merge_annotations,
    _plan_meta,
    adapters,
    iter_antigravity_items,
    iter_cc_like_items,
    iter_claude_science_items,
    iter_codex_items,
    iter_cowork_items,
    iter_cursor_items,
    iter_grok_items,
    iter_opencode_items,
    main,
    plan_thread,
    run,
)
from thread_archive._scripts.backfill_reconcile import _fresh_events as cc_fresh
from thread_archive._store import Event, ImportState, Thread, get_session, init_db
from thread_archive._thread_import.event_builder import ThreadEvent, compute_dedup_key

from .helpers import install_cc_shaped_plugin

# ── Claude-Code-shaped harness (lines fixture) ───────────────────────────────

USAGE = {
    "input_tokens": 11, "output_tokens": 7, "thinking_tokens": 0,
    "cache_read_tokens": 1200, "cache_write_tokens": 300,
}

CC_LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "hello there"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m", "usage": dict(USAGE),
                                    "cost": 0.0421,
                                    "content": [{"type": "text", "text": "hi back"}]}},
]


@pytest.fixture
def demo_harness(archive_home, monkeypatch):
    """A registered provider whose harness writes Claude-Code-shaped JSONL,
    installed the way a plugin author installs one — declared in ``config.json``
    and discovered at registry build.

    The audit derives its Claude-Code-shaped adapters from the registry, so a
    source is re-parsed exactly when a provider declares that parser. Registering
    one is what makes the audit reach a source outside the built-in set — a
    source with no adapter isn't audited, and nothing reports that it wasn't.
    """
    from thread_archive import _providers

    install_cc_shaped_plugin(archive_home, monkeypatch, stores={},
                             watcherless=["demo-harness"])
    yield _providers.get("demo-harness")
    _providers.reset()


def _import_demo_harness(archive_home):
    init_db()
    f = archive_home / "s1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in CC_LINES) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, "s1", source="demo-harness").thread_id
    return f, tid


def _demo_harness_items(f):
    return [WorkItem("demo-harness", "s1",
                     fresh=lambda: cc_fresh("demo-harness", read_session_lines(f)),
                     meta=None)]


def _event(tid, event_type):
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == event_type
        )).scalars().one()
        return ev.id, dict(ev.payload), ev.dedup_key


def _set_payload(eid, payload, dedup_key=None):
    with get_session() as s:
        values = {"payload": payload}
        if dedup_key is not None:
            values["dedup_key"] = dedup_key
        s.execute(update(Event).where(Event.id == eid).values(**values))
        s.commit()


def test_missing_keys_zero_tokens_dry_run_and_idempotency(archive_home, demo_harness) -> None:
    f, tid = _import_demo_harness(archive_home)
    eid, payload, key = _event(tid, "api_request_completed")
    # OLD shape: cost + cache counts dropped, input_tokens the 0 placeholder.
    old = {k: v for k, v in payload.items()
           if k not in ("cost", "cache_read_tokens", "cache_write_tokens")}
    old["input_tokens"] = 0
    _set_payload(eid, old)

    dry = run(apply=False, items=_demo_harness_items(f))
    t = dry["totals"]
    assert t["patches"] == 1
    assert t["field:cost"] == 1 and t["field:cache_read_tokens"] == 1
    assert t["field:input_tokens"] == 1  # 0 → 11 placeholder overwrite
    # Dry-run wrote nothing — payload untouched, no amendment records.
    with get_session() as s:
        assert "cost" not in s.get(Event, eid).payload
        assert s.get(Event, eid).payload["input_tokens"] == 0
    assert load_amendments() == []

    applied = run(apply=True, items=_demo_harness_items(f))
    assert applied["totals"]["events_amended"] == 1
    with get_session() as s:
        p = s.get(Event, eid).payload
        assert p["cost"] == 0.0421
        assert p["cache_read_tokens"] == 1200 and p["cache_write_tokens"] == 300
        assert p["input_tokens"] == 11
        assert s.get(Event, eid).dedup_key == key  # identity untouched

    again = run(apply=True, items=_demo_harness_items(f))
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"]["already_complete"] >= 1


# ── opencode (tmp SQLite) ────────────────────────────────────────────────────

def _make_opencode_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
        "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)"
    )
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    sid = "s1"
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
        (sid, "proj", None, "OC Session", "/proj", 1700000000000, 1700000005000, "build"),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m1", sid, 1700000000000, json.dumps({"role": "user", "time": {"created": 1700000000000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p1", "m1", sid, 1700000000000,
         json.dumps({"type": "text", "text": "hello opencode", "time": {"start": 1700000000000}})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m2", sid, 1700000001000, json.dumps(
            {"role": "assistant", "modelID": "gpt", "providerID": "oai",
             "time": {"created": 1700000001000, "completed": 1700000004000},
             "cost": 0.01,
             "tokens": {"input": 5, "output": 7, "cache": {"read": 100}},
             "finish": "stop",
             "error": {"name": "E", "data": {"message": "boom"}}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p2", "m2", sid, 1700000001000,
         json.dumps({"type": "text", "text": "hi from opencode",
                     "time": {"start": 1700000001000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p3", "m2", sid, 1700000002000,
         json.dumps({"type": "tool", "callID": "c1", "tool": "bash",
                     "state": {"status": "error", "input": {"cmd": "x"},
                               "error": "perm denied",
                               "metadata": {"exit": 1},
                               "time": {"start": 1700000002000, "end": 1700000003000}}})),
    )
    conn.commit()
    conn.close()


def test_opencode_annotations_deep_merge_error_salvage_and_meta(archive_home) -> None:
    init_db()
    db = archive_home / "opencode.db"
    _make_opencode_db(db)
    assert import_opencode_db(db).imported == 1
    with get_session() as s:
        tid = s.execute(select(Thread.id).where(Thread.source == "opencode")).scalar_one()

    # OLD shape for api_request_completed: no cost, zero input_tokens, and an
    # annotations dict with a pre-existing subkey that must survive the merge.
    eid, payload, _key = _event(tid, "api_request_completed")
    old = {k: v for k, v in payload.items() if k != "cost"}
    old["input_tokens"] = 0
    old["annotations"] = {"custom": True}
    _set_payload(eid, old)

    # OLD shape for the tool error: hollow error text, no annotations — its old
    # content differs, so its old dedup_key was computed on the hollow payload.
    err_id, err_payload, _ = _event(tid, "tool_execution_error")
    hollow = {k: v for k, v in err_payload.items() if k != "annotations"}
    hollow["error"] = ""
    _set_payload(err_id, hollow,
                 dedup_key=compute_dedup_key("m2", "tool_execution_error", hollow))

    # Thread metadata the old importer never derived.
    with get_session() as s:
        thread = s.get(Thread, tid)
        meta = dict(thread.source_metadata or {})
        meta.pop("project_id", None)
        meta.pop("agent", None)
        thread.source_metadata = meta
        s.commit()

    items = list(iter_opencode_items(db_path=db))
    assert len(items) == 1

    applied = run(apply=True, items=items)
    t = applied["totals"]
    assert t["field:cost"] == 1 and t["field:input_tokens"] == 1
    assert t["field:annotations"] == 1
    assert t["salvage:error_detail"] == 1
    assert t["meta_keys_added"] == 2

    with get_session() as s:
        p = s.get(Event, eid).payload
        assert p["cost"] == 0.01 and p["input_tokens"] == 5
        # Deep merge: missing subkey added, existing subkey untouched.
        assert p["annotations"]["custom"] is True
        assert p["annotations"]["error"]["name"] == "E"
        ep = s.get(Event, err_id).payload
        assert ep["error"] == ""  # content field never touched
        assert ep["annotations"]["error_detail"] == "perm denied"
        sm = s.get(Thread, tid).source_metadata
        assert sm["project_id"] == "proj" and sm["agent"] == "build"

    # Idempotent: nothing left missing; salvage recognizes its own annotation.
    again = run(apply=True, items=items)
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"]["salvage_already"] == 1
    assert again["totals"].get("meta_keys_added", 0) == 0


# ── antigravity (insert allowlist + stream borrowing) ────────────────────────

AG_LINES = [
    {"source": "USER_EXPLICIT", "type": "USER_INPUT",
     "created_at": "2026-01-01T10:00:00Z",
     "content": "<USER_REQUEST>fix it</USER_REQUEST>"},
    {"source": "MODEL", "type": "PLANNER_RESPONSE",
     "created_at": "2026-01-01T10:00:05Z",
     "thinking": "The bug is in the loop bound.", "content": "On it."},
]


def _make_ag_session(root, session_id):
    d = root / session_id / ".system_generated" / "logs"
    d.mkdir(parents=True)
    f = d / "transcript.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in AG_LINES) + "\n", encoding="utf-8")
    return f


def test_antigravity_insert_borrows_stream_and_api_ids(archive_home) -> None:
    init_db()
    root = archive_home / "brain"
    f = _make_ag_session(root, "ag1")
    tid = import_antigravity_session_incremental(f, "ag1").thread_id

    # OLD store: the thinking event was never written.
    with get_session() as s:
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type == "thinking_complete"
        ))
        s.commit()
        started = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "api_request_started"
        )).scalars().one()
        want_stream, want_api = started.stream_id, started.api_call_id

    items = list(iter_antigravity_items(brain_dir=root))
    dry = run(apply=False, items=items)
    assert dry["totals"]["inserts"] == 1
    assert dry["totals"]["insert:thinking_complete"] == 1
    with get_session() as s:  # dry-run inserted nothing
        assert s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "thinking_complete"
        )).scalars().first() is None

    applied = run(apply=True, items=list(iter_antigravity_items(brain_dir=root)),
                  backup_path=archive_home / "backup.jsonl")
    assert applied["totals"]["events_inserted"] == 1
    with get_session() as s:
        th = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "thinking_complete"
        )).scalars().one()
        assert th.payload["text"] == "The bug is in the loop bound."
        assert th.stream_id == want_stream and th.api_call_id == want_api
        assert th.dedup_key
    # Inserted ids landed in the backup JSONL.
    recs = [json.loads(x) for x in
            (archive_home / "backup.jsonl").read_text().splitlines()]
    assert recs and recs[0]["event_ids"] == [th.id]

    again = run(apply=True, items=list(iter_antigravity_items(brain_dir=root)))
    assert again["totals"].get("inserts", 0) == 0
    assert again["totals"].get("patches", 0) == 0


def test_antigravity_unanchored_insert_skipped(archive_home) -> None:
    init_db()
    root = archive_home / "brain"
    f = _make_ag_session(root, "ag2")
    tid = import_antigravity_session_incremental(f, "ag2").thread_id
    with get_session() as s:  # nothing stored at all → no turn to borrow from
        s.execute(delete(Event).where(Event.thread_id == tid))
        s.commit()

    res = run(apply=True, items=list(iter_antigravity_items(brain_dir=root)))
    assert res["totals"]["insert_unanchored_skipped"] == 1
    assert res["totals"].get("inserts", 0) == 0
    with get_session() as s:
        assert s.execute(select(Event).where(Event.thread_id == tid)).scalars().first() is None


# ── cursor (tmp state.vscdb, model placeholder salvage) ──────────────────────

def _make_cursor_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    composer = {
        "name": "Cursor Chat",
        "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                        {"bubbleId": "b2", "type": 2}],
    }
    rows = [
        ("composerData:comp1", json.dumps(composer)),
        ("bubbleId:comp1:b1", json.dumps(
            {"type": 1, "text": "hello cursor", "createdAt": 1700000000000})),
        ("bubbleId:comp1:b2", json.dumps(
            {"type": 2, "text": "hi from cursor", "createdAt": 1700000001000,
             "modelInfo": {"modelName": "gpt-5-turbo"},
             "tokenCount": {"inputTokens": 10, "outputTokens": 5}})),
    ]
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_cursor_model_placeholder_salvage(archive_home) -> None:
    init_db()
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)
    assert import_cursor_db(db).imported == 1
    with get_session() as s:
        tid = s.execute(select(Thread.id).where(Thread.source == "cursor")).scalar_one()

    # OLD store: model was the "cursor" placeholder — content differed, so the
    # old rows carried keys computed on the placeholder payload.
    for etype in ("api_request_started", "api_request_completed"):
        eid, payload, _ = _event(tid, etype)
        old = dict(payload)
        old["model"] = "cursor"
        _set_payload(eid, old, dedup_key=compute_dedup_key("b2", etype, old))

    items = list(iter_cursor_items(db_path=db))
    dry = run(apply=False, items=items)
    assert dry["totals"]["salvage:model_name"] == 2

    run(apply=True, items=items)
    with get_session() as s:
        for etype in ("api_request_started", "api_request_completed"):
            ev = s.execute(select(Event).where(
                Event.thread_id == tid, Event.event_type == etype
            )).scalars().one()
            assert ev.payload["model"] == "cursor"  # content stays untouched
            assert ev.payload["annotations"]["model_name"] == "gpt-5-turbo"

    again = run(apply=True, items=items)
    assert again["totals"]["salvage_already"] == 2
    assert again["totals"].get("patches", 0) == 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_main_prints_dry_run_table(archive_home, capsys, demo_harness) -> None:
    f, tid = _import_demo_harness(archive_home)
    eid, payload, _ = _event(tid, "api_request_completed")
    _set_payload(eid, {k: v for k, v in payload.items() if k != "cost"})
    main(["--limit", "5", "--source", "demo-harness"], items=_demo_harness_items(f))
    out = capsys.readouterr().out
    assert "[DRY-RUN] backfill-dropped-fields" in out
    assert "field:cost" in out
    assert "demo-harness" in out


def test_main_apply_prints_write_counters(archive_home, capsys, demo_harness) -> None:
    """The apply footer reports what was written, not what was planned."""
    f, tid = _import_demo_harness(archive_home)
    eid, payload, _ = _event(tid, "api_request_completed")
    _set_payload(eid, {k: v for k, v in payload.items() if k != "cost"})
    main(["--apply", "-v"], items=_demo_harness_items(f))
    out = capsys.readouterr().out
    assert "[APPLIED] backfill-dropped-fields" in out
    assert "events amended:      1" in out
    assert "events inserted:     0" in out
    assert "meta keys added:     0" in out
    assert "inserts planned" not in out  # the dry-run footer stays out of an apply
    with get_session() as s:
        assert s.get(Event, eid).payload["cost"] == 0.0421


# ── hand-built stores (for the arms no importer fixture reaches) ─────────────

BASE_TS = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _seed_thread(source, source_id, events, *, source_metadata=None) -> int:
    """Seed a thread + import_state + hand-built stored events; returns the thread id.

    Each event dict carries event_type / payload and optionally stream_id,
    api_call_id, occurred_at, dedup_key."""
    init_db()
    with get_session() as s:
        t = Thread(name=f"dropped-fields-{source_id}", source=source,
                   source_metadata=source_metadata)
        s.add(t)
        s.flush()
        tid = t.id
        s.add(ImportState(source=source, source_id=source_id, thread_id=tid))
        for e in events:
            s.add(Event(
                thread_id=tid,
                stream_id=e.get("stream_id", "st"),
                api_call_id=e.get("api_call_id"),
                event_type=e["event_type"],
                payload=e["payload"],
                occurred_at=e.get("occurred_at", BASE_TS),
                dedup_key=e.get("dedup_key"),
            ))
        s.commit()
    return tid


def _fresh_event(event_type, payload, *, dedup_key=None, stream_id="fs",
                 api_call_id=None, occurred_at=BASE_TS) -> ThreadEvent:
    """One freshly-built event, as a re-parse would hand it to the planner."""
    return ThreadEvent(event_type=event_type, payload=payload, stream_id=stream_id,
                       api_call_id=api_call_id, occurred_at=occurred_at,
                       dedup_key=dedup_key)


def _plan(tid, source, fresh):
    with get_session() as s:
        return plan_thread(s, tid, source, fresh)


# ── patch construction (pure) ────────────────────────────────────────────────

def test_merge_annotations_shapes() -> None:
    """Add-only, stored-wins, and every shape that forbids a merge outright."""
    assert _merge_annotations(None, {"a": 1}) == {"a": 1}       # nothing stored → take it
    assert _merge_annotations({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _merge_annotations({"a": 1}, {"a": 9}) is None       # stored subkey wins
    assert _merge_annotations({"a": 1}, {}) is None             # nothing fresh to add
    assert _merge_annotations({"a": 1}, "not-a-dict") is None
    assert _merge_annotations("not-a-dict", {"a": 1}) is None   # unexpected stored shape


def test_insertable_allowlist() -> None:
    """Only the three event types the retired importers never wrote at all."""
    assert _insertable("antigravity", _fresh_event("thinking_complete", {"text": "t"}))
    assert not _insertable("codex", _fresh_event("thinking_complete", {"text": "t"}))
    assert _insertable("cursor", _fresh_event("context_summary", {"content": "c"}))
    assert not _insertable("cursor", _fresh_event("text_complete", {"text": "t"}))
    # codex: only an image-only user turn — a captioned one was always written.
    img = {"images": [{"media_type": "image/png", "data": "x"}], "content": ""}
    assert _insertable("codex", _fresh_event("user_message_sent", img))
    assert not _insertable(
        "codex", _fresh_event("user_message_sent", {**img, "content": "look"}))
    assert not _insertable("codex", _fresh_event("user_message_sent", {"content": ""}))


# ── plan_thread edge branches ────────────────────────────────────────────────

def test_plan_pmid_disagreement_falls_through_to_drift(archive_home) -> None:
    """Identical content at the same instant but a different provider_message_id is
    a different turn: the content anchor refuses it, the structural anchor matches,
    and a drift with no salvage rule is counted rather than merged."""
    tid = _seed_thread("codex", "c1", [
        {"event_type": "text_complete",
         "payload": {"block_index": 0, "text": "shared",
                     "provider_data": {"provider_message_id": "pm-stored"}},
         "dedup_key": "stored-key"},
    ])
    plan = _plan(tid, "codex", [_fresh_event(
        "text_complete",
        {"block_index": 0, "text": "shared", "cost": 0.5,
         "provider_data": {"provider_message_id": "pm-fresh"}},
        dedup_key="fresh-key")])
    assert plan.stats["content_drift_skipped"] == 1
    assert plan.patches == []


def test_plan_ambiguous_content_candidates_are_never_guessed(archive_home) -> None:
    """Two stored rows share one content anchor — the fresh event could be either,
    so it is counted ambiguous and skipped (no salvage, no insert)."""
    row = {"event_type": "text_complete", "payload": {"block_index": 0, "text": "twin"},
           "dedup_key": None}
    tid = _seed_thread("codex", "c-amb", [dict(row), dict(row)])
    plan = _plan(tid, "codex", [_fresh_event(
        "text_complete", {"block_index": 0, "text": "twin", "cost": 0.5})])
    assert plan.stats["ambiguous"] == 1
    assert plan.patches == [] and plan.inserts == []


def test_plan_ambiguous_structural_candidates_are_never_guessed(archive_home) -> None:
    """Two stored rows share a structural anchor and neither matches on content —
    the salvage phase refuses to pick one."""
    tid = _seed_thread("cursor", "c-struct", [
        {"event_type": "text_complete", "payload": {"block_index": 0, "text": "a"},
         "dedup_key": "ka"},
        {"event_type": "text_complete", "payload": {"block_index": 0, "text": "b"},
         "dedup_key": "kb"},
    ])
    plan = _plan(tid, "cursor", [_fresh_event(
        "text_complete", {"block_index": 0, "text": "c"}, dedup_key="kc")])
    assert plan.stats["struct_ambiguous"] == 1
    assert plan.patches == []


def test_plan_one_stored_row_is_claimed_by_only_one_fresh_event(archive_home) -> None:
    """A stored row already consumed by an earlier fresh event is out of the running
    for the next one — the second fresh event stays unmatched rather than
    double-patching the same row."""
    tid = _seed_thread("codex", "c-used", [
        {"event_type": "text_complete", "payload": {"block_index": 0, "text": "same"},
         "dedup_key": None},
    ])
    twin = {"block_index": 0, "text": "same", "cost": 0.25}
    plan = _plan(tid, "codex", [_fresh_event("text_complete", dict(twin)),
                                _fresh_event("text_complete", dict(twin))])
    assert len(plan.patches) == 1
    assert plan.stats["unmatched_fresh"] == 1


def test_plan_duplicate_fresh_key_counted_not_repatched(archive_home) -> None:
    """Two fresh events carrying one dedup_key resolve to the same stored row; the
    second is counted ``dup_fresh``."""
    tid = _seed_thread("codex", "c-dup", [
        {"event_type": "api_request_completed", "payload": {"input_tokens": 3},
         "dedup_key": "k1"},
    ])
    plan = _plan(tid, "codex", [
        _fresh_event("api_request_completed", {"input_tokens": 3, "cost": 0.1},
                     dedup_key="k1"),
        _fresh_event("api_request_completed", {"input_tokens": 3, "cost": 0.1},
                     dedup_key="k1"),
    ])
    assert plan.stats["dup_fresh"] == 1
    assert len(plan.patches) == 1


def test_plan_refuses_to_patch_a_redacted_payload(archive_home) -> None:
    """A redaction marker carries no fields to merge into — amend's ``check_patch``
    refuses, and the planner counts it instead of writing."""
    tid = _seed_thread("codex", "c-redacted", [
        {"event_type": "api_request_completed",
         "payload": {"_redacted": {"reason": "pii"}}, "dedup_key": "k1"},
    ])
    plan = _plan(tid, "codex", [
        _fresh_event("api_request_completed", {"cost": 0.5}, dedup_key="k1"),
    ])
    assert plan.stats["patch_refused"] == 1
    assert plan.patches == []


def test_plan_insert_keys_are_deduped_and_keyless_inserts_allowed(archive_home) -> None:
    """The insert phase borrows its turn's stored ids, plans one row per fresh
    dedup_key, and still inserts a fresh event that carries no key at all."""
    tid = _seed_thread("antigravity", "ag-ins", [
        {"event_type": "api_request_started", "payload": {"model": "m"},
         "stream_id": "STORED-STREAM", "api_call_id": "STORED-API",
         "dedup_key": "anchor"},
    ])
    anchor = _fresh_event("api_request_started", {"model": "m", "cost": 0.2},
                          dedup_key="anchor", stream_id="fs", api_call_id="fa")
    thinking = {"block_index": 0, "text": "the bug is the loop bound"}
    plan = _plan(tid, "antigravity", [
        anchor,
        _fresh_event("thinking_complete", dict(thinking), dedup_key="tk",
                     stream_id="fs", api_call_id="fa"),
        _fresh_event("thinking_complete", dict(thinking), dedup_key="tk",
                     stream_id="fs", api_call_id="fa"),
        _fresh_event("thinking_complete", {"block_index": 1, "text": "and again"},
                     stream_id="fs", api_call_id="fa"),
    ])
    assert plan.stats["insert:thinking_complete"] == 2
    assert plan.stats["already_complete"] == 1  # the repeated key, planned once
    assert [e.stream_id for e in plan.inserts] == ["STORED-STREAM"] * 2
    assert [e.api_call_id for e in plan.inserts] == ["STORED-API"] * 2
    assert [e.dedup_key for e in plan.inserts] == ["tk", None]


# ── metadata merge ───────────────────────────────────────────────────────────

def test_plan_meta_handles_no_metadata_and_a_missing_thread(archive_home) -> None:
    init_db()
    tid = _seed_thread("opencode", "oc-meta", [], source_metadata={"agent": "build"})
    with get_session() as s:
        assert _plan_meta(s, tid, None) == {}                     # nothing derived
        assert _plan_meta(s, tid, {"agent": "other"}) == {}       # existing key wins
        assert _plan_meta(s, tid, {"directory": None}) == {}      # None is not a value
        assert _plan_meta(s, tid, {"directory": "/p"}) == {"directory": "/p"}
        assert _plan_meta(s, 999999, {"directory": "/p"}) == {}   # thread vanished


def test_apply_thread_meta_merge_is_missing_only_and_survives_a_gone_thread(
    archive_home,
) -> None:
    """Applying a stale metadata plan adds nothing when the key arrived meanwhile,
    and does not raise when the thread itself is gone."""
    tid = _seed_thread("opencode", "oc-apply", [], source_metadata={"agent": "build"})
    stale = ThreadPlan(tid, [], [], {"agent": "other"}, {})
    assert _apply_thread(tid, "opencode", "oc-apply", stale, None) == {}
    with get_session() as s:
        assert s.get(Thread, tid).source_metadata == {"agent": "build"}

    gone = ThreadPlan(999999, [], [], {"agent": "build"}, {})
    assert _apply_thread(999999, "opencode", "oc-apply", gone, None) == {}


# ── run: source filter, limit, and the error/skip arms ───────────────────────

def _noop_item(source, source_id="s1"):
    return WorkItem(source, source_id, fresh=lambda: [], meta=None)


def test_run_skips_items_outside_the_source_filter(archive_home, demo_harness) -> None:
    f, _tid = _import_demo_harness(archive_home)
    res = run(apply=False, sources={"codex"}, items=_demo_harness_items(f))
    assert res["sources"] == {}
    assert res["totals"] == {}


def test_run_limit_is_per_source(archive_home, demo_harness) -> None:
    """``--limit`` caps threads examined per source, so a capped sweep still
    samples every source."""
    f, _tid = _import_demo_harness(archive_home)
    items = _demo_harness_items(f) * 3 + [_noop_item("codex", "no-such-codex")]
    res = run(apply=False, limit=1, items=items)
    assert res["sources"]["demo-harness"]["threads"] == 1
    assert res["sources"]["codex"]["no_thread"] == 1  # reached despite the cap


def test_run_counts_items_with_no_imported_thread(archive_home) -> None:
    init_db()
    res = run(apply=False, items=[_noop_item("codex", "never-imported")])
    assert res["totals"]["no_thread"] == 1
    assert res["totals"].get("threads", 0) == 0


def test_run_counts_plan_errors_without_stopping_the_sweep(
    archive_home, demo_harness
) -> None:
    """One unreadable source must not abort the others."""
    f, _tid = _import_demo_harness(archive_home)

    def _boom():
        raise RuntimeError("re-parse blew up")

    items = [WorkItem("demo-harness", "s1", fresh=_boom, meta=None),
             *_demo_harness_items(f)]
    res = run(apply=False, items=items)
    st = res["sources"]["demo-harness"]
    assert st["plan_errors"] == 1
    assert st["threads"] == 2  # the second item was still examined


def test_run_counts_apply_errors_and_keeps_going(archive_home, demo_harness) -> None:
    """A thread whose write fails is counted and skipped, not fatal. The amendments
    go through their own locked seam first, so a later insert failure rolls back its
    own transaction only — and the next source is still swept."""
    f, tid = _import_demo_harness(archive_home)
    ag_tid = _seed_thread("antigravity", "ag-bad", [
        {"event_type": "api_request_started", "payload": {"model": "m"},
         "stream_id": "STORED-STREAM", "api_call_id": "STORED-API",
         "dedup_key": "anchor"},
    ])
    eid, payload, _ = _event(tid, "api_request_completed")
    _set_payload(eid, {k: v for k, v in payload.items() if k != "cost"})

    def _fresh_with_an_unstorable_insert():
        return [
            _fresh_event("api_request_started", {"model": "m", "cost": 0.2},
                         dedup_key="anchor", stream_id="fs", api_call_id="fa"),
            # a payload the store can't serialize — the insert fails at write time
            _fresh_event("thinking_complete",
                         {"block_index": 0, "text": "t", "junk": {1, 2}},
                         dedup_key="tk", stream_id="fs", api_call_id="fa"),
        ]

    items = [WorkItem("antigravity", "ag-bad",
                      fresh=_fresh_with_an_unstorable_insert, meta=None),
             *_demo_harness_items(f)]
    res = run(apply=True, items=items)
    assert res["sources"]["antigravity"]["apply_errors"] == 1
    assert res["sources"]["antigravity"].get("events_inserted", 0) == 0
    assert res["sources"]["demo-harness"]["events_amended"] == 1  # swept regardless
    with get_session() as s:
        anchor = s.execute(select(Event).where(
            Event.thread_id == ag_tid, Event.event_type == "api_request_started"
        )).scalars().one()
        assert anchor.payload["cost"] == 0.2  # its amendment had already committed
        assert s.execute(select(Event).where(
            Event.thread_id == ag_tid, Event.event_type == "thinking_complete"
        )).scalars().first() is None  # the failed insert rolled back
        assert s.get(Event, eid).payload["cost"] == 0.0421


# ── adapter registry / default discovery ─────────────────────────────────────

def test_adapters_cover_bespoke_sources_and_registry_cc_sources(demo_harness) -> None:
    """Every bespoke source has an adapter, and a provider declaring the Claude
    Code parser gets one derived from the registry — that derivation is what stops
    a same-format harness from going silently un-audited."""
    found = adapters()
    assert set(bdf._BESPOKE_ADAPTERS) <= set(found)
    assert "claude-code" in found
    assert "demo-harness" in found  # registered by the fixture, not hardcoded


def test_run_default_discovery_counts_unavailable_sources(archive_home, monkeypatch) -> None:
    """Without scripted items, ``run`` walks the adapters it derives from the
    registry: a source whose store holds a transcript the archive never imported is
    counted as a thread it can't reach, a source whose store is empty is counted
    unavailable, and a source outside the filter is never walked at all."""
    from thread_archive import _providers

    yielding, empty, filtered = (archive_home / n for n in ("yielding", "empty", "filtered"))
    for root in (yielding, empty, filtered):
        root.mkdir()
    (yielding / "never-imported.jsonl").write_text(
        "\n".join(json.dumps(x) for x in CC_LINES) + "\n", encoding="utf-8")
    (filtered / "also-never-imported.jsonl").write_text(
        "\n".join(json.dumps(x) for x in CC_LINES) + "\n", encoding="utf-8")

    install_cc_shaped_plugin(archive_home, monkeypatch, stores={
        "yielding": yielding, "empty": empty, "filtered-out": filtered})
    try:
        init_db()
        res = run(apply=False, sources={"yielding", "empty"})
        assert res["sources"]["empty"]["source_unavailable_or_empty"] == 1
        assert res["sources"]["yielding"]["no_thread"] == 1
        assert "source_unavailable_or_empty" not in res["sources"]["yielding"]
        # the filtered source was never walked — it has a transcript on disk and
        # would have reported one too, had the filter let the adapter run
        assert "filtered-out" not in res["sources"]
    finally:
        _providers.reset()


def test_iter_cc_like_items_selects_only_its_own_source(archive_home, monkeypatch) -> None:
    """The shared Claude-Code-shaped adapter walks every such provider's transcripts
    and keeps the ones belonging to the source it was asked about — lazily,
    re-parsing the file only when ``fresh()`` is called."""
    from thread_archive import _providers

    text = "\n".join(json.dumps(x) for x in CC_LINES) + "\n"
    roots = {}
    for name in ("store-a", "store-b"):
        root = archive_home / name
        root.mkdir()
        (root / f"sid-{name[-1]}.jsonl").write_text(text, encoding="utf-8")
        roots[name] = root
    install_cc_shaped_plugin(archive_home, monkeypatch, stores=roots)
    try:
        items = list(iter_cc_like_items("store-b"))
        assert [(i.source, i.source_id) for i in items] == [("store-b", "sid-b")]
        assert items[0].meta is None
        types = {e.event_type for e in items[0].fresh()}
        assert "user_message_sent" in types and "api_request_completed" in types
    finally:
        _providers.reset()


# ── fresh event assembly ─────────────────────────────────────────────────────

def test_events_from_messages_mirrors_assemble_events_stream_and_clock_rules() -> None:
    """Matching is anchored on content, never on stream ids, but an insert borrows
    them — so the fresh build must group turns exactly as ``assemble_events`` does:
    a user message opens a stream that its following turns share, an assistant with
    no user turn before it opens its own, and a message that builds no events leaves
    the monotonic clock where the last built event put it."""
    def _msg(role, text, created_at=None):
        msg = {"role": role, "content_text": text,
               "content_blocks": [{"type": "text", "text": text}]}
        if created_at:
            msg["created_at"] = created_at
        return msg

    evs = bdf._events_from_messages([
        _msg("assistant", "orphan", "2026-01-01T10:00:00Z"),
        _msg("user", "hi", "2026-01-01T10:00:05Z"),
        _msg("tool", "note", "2026-01-01T10:00:06Z"),   # a role the builder doesn't model
        {"role": "user", "content_text": "", "content_blocks": [],
         "created_at": "2026-01-01T10:00:07Z"},         # builds nothing at all
        _msg("assistant", "hello"),                     # no source timestamp
    ])
    by_type = {}
    for e in evs:
        by_type.setdefault(e.event_type, []).append(e)

    orphan, replied = by_type["api_request_started"]
    assert orphan.stream_id != by_type["user_message_sent"][0].stream_id
    # the unmodeled role rides the open user stream rather than stranding itself
    assert by_type["message"][0].stream_id == by_type["user_message_sent"][0].stream_id
    # the eventless user turn still opens a stream (as the importer does), and the
    # assistant after it inherits the last *built* event's time, not that turn's
    assert replied.stream_id not in {orphan.stream_id,
                                     by_type["user_message_sent"][0].stream_id}
    assert replied.occurred_at == by_type["message"][0].occurred_at
    assert replied.payload["timestamp_inferred"] is True


# ── salvage patch (pure) ─────────────────────────────────────────────────────

def test_salvage_patch_arms() -> None:
    """Only a hollow stored error and the ``cursor`` model placeholder are
    salvageable; anything else is plain drift and is left alone."""
    def _stored(event_type, payload):
        return Event(event_type=event_type, payload=payload, stream_id="st")

    hollow = _stored("tool_execution_error", {"error": "   "})
    patch, reason = bdf._salvage_patch(hollow, _fresh_event(
        "tool_execution_error", {"error": "perm denied"}))
    assert reason == "error_detail" and patch == {"annotations": {"error_detail": "perm denied"}}
    # a stored error that isn't hollow is real content drift, not a salvage
    real = _stored("tool_execution_error", {"error": "old text"})
    assert bdf._salvage_patch(real, _fresh_event(
        "tool_execution_error", {"error": "new text"})) == (None, "drift")
    # ...and so is a fresh error with nothing in it
    assert bdf._salvage_patch(hollow, _fresh_event(
        "tool_execution_error", {"error": ""})) == (None, "drift")
    already = _stored("tool_execution_error",
                      {"error": "", "annotations": {"error_detail": "kept"}})
    assert bdf._salvage_patch(already, _fresh_event(
        "tool_execution_error", {"error": "new"})) == (None, "already")

    placeholder = _stored("api_request_completed", {"model": "cursor"})
    patch, reason = bdf._salvage_patch(placeholder, _fresh_event(
        "api_request_completed", {"model": "gpt-5"}))
    assert reason == "model_name" and patch == {"annotations": {"model_name": "gpt-5"}}
    # a real stored model name is never second-guessed
    named = _stored("api_request_started", {"model": "gpt-4"})
    assert bdf._salvage_patch(named, _fresh_event(
        "api_request_started", {"model": "gpt-5"})) == (None, "drift")
    # nor is a placeholder the fresh parse also failed to resolve
    assert bdf._salvage_patch(placeholder, _fresh_event(
        "api_request_started", {"model": "cursor"})) == (None, "drift")
    # a non-dict stored payload has nothing to salvage onto
    assert bdf._salvage_patch(
        _stored("text_complete", "not-a-dict"),
        _fresh_event("text_complete", {"text": "x"})) == (None, "drift")


# ── opencode / cursor adapter guards ─────────────────────────────────────────

def test_iter_opencode_items_skips_absent_and_schemaless_stores(archive_home) -> None:
    assert list(iter_opencode_items(db_path=archive_home / "nope.db")) == []
    assert list(iter_opencode_items()) == []  # no store on this (sandbox) machine
    empty = archive_home / "empty.db"
    sqlite3.connect(empty).close()
    assert list(iter_opencode_items(db_path=empty)) == []  # no session/message/part


def test_iter_opencode_items_skips_corrupt_rows(archive_home) -> None:
    """A row the importer couldn't parse either is skipped, not fatal — the rest of
    the session still re-parses."""
    db = archive_home / "opencode.db"
    _make_opencode_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("m9", "s1", 1700000009000, "{not json"))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                 ("p9", "m2", "s1", 1700000009000, None))
    conn.commit()
    conn.close()

    items = list(iter_opencode_items(db_path=db))
    assert [i.source_id for i in items] == ["s1"]
    assert {e.event_type for e in items[0].fresh()} >= {"user_message_sent",
                                                       "api_request_completed"}
    assert items[0].meta() == {"project_id": "proj", "directory": "/proj",
                               "agent": "build"}


def test_iter_cursor_items_skips_absent_and_schemaless_stores(archive_home) -> None:
    assert list(iter_cursor_items(db_path=archive_home / "nope.vscdb")) == []
    empty = archive_home / "empty.vscdb"
    sqlite3.connect(empty).close()
    assert list(iter_cursor_items(db_path=empty)) == []  # no cursorDiskKV table


def test_iter_cursor_items_skips_corrupt_and_malformed_keys(archive_home) -> None:
    """Undecodable composer/bubble blobs and a truncated bubble key are skipped;
    the well-formed composer still re-parses."""
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        ("composerData:broken", "{not json"),
        ("bubbleId:comp1", json.dumps({"type": 1, "text": "no bubble id"})),
        ("bubbleId:comp1:b9", "{not json"),
    ])
    conn.commit()
    conn.close()

    items = list(iter_cursor_items(db_path=db))
    assert [i.source_id for i in items] == ["comp1"]
    assert items[0].meta is None
    assert {e.event_type for e in items[0].fresh()} >= {"user_message_sent",
                                                       "api_request_completed"}


# ── codex (rollout files: image-only insert + session_meta merge) ────────────

_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
            "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

CODEX_LINES = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "sess", "cwd": "/proj", "cli_version": "1.2.3",
                 "git": {"branch": "main", "commit_hash": "abc123"}}},
    # An uncaptioned screenshot paste — a turn the retired importer dropped whole.
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "", "turn_id": "t1",
                 "images": [f"data:image/png;base64,{_PNG_B64}"]}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "agent_message", "message": "i see it"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:03Z",
     "payload": {"type": "token_count", "info": {"last_token_usage": {
         "input_tokens": 100, "cached_input_tokens": 60,
         "output_tokens": 20, "reasoning_output_tokens": 5}}}},
]


def _make_codex_session(root, source_id="cx1"):
    root.mkdir(parents=True, exist_ok=True)
    f = root / f"{source_id}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in CODEX_LINES) + "\n", encoding="utf-8")
    return f


def test_iter_codex_items_skips_an_absent_sessions_dir(archive_home) -> None:
    assert list(iter_codex_items(sessions_dir=archive_home / "nope")) == []


def test_codex_image_only_turn_inserted_and_session_meta_merged(archive_home) -> None:
    """The codex adapter re-parses a rollout end to end: the image-only user turn
    the old importer never wrote is inserted into its stored turn, the zero-token
    placeholder is replaced, and the session_meta git provenance lands on the
    thread. Applied without a backup path — the insert still writes."""
    init_db()
    root = archive_home / "codex-sessions"
    f = _make_codex_session(root)
    tid = import_codex_session_incremental(f, "cx1").thread_id

    with get_session() as s:
        started = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "api_request_started"
        )).scalars().one()
        want_stream = started.stream_id
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type == "user_message_sent"))
        thread = s.get(Thread, tid)
        thread.source_metadata = {k: v for k, v in (thread.source_metadata or {}).items()
                                  if k not in ("git", "cli_version")}
        s.commit()
    comp_id, payload, _ = _event(tid, "api_request_completed")
    _set_payload(comp_id, {**payload, "input_tokens": 0})

    items = list(iter_codex_items(sessions_dir=root))
    assert [(i.source, i.source_id) for i in items] == [("codex", "cx1")]

    applied = run(apply=True, items=items)
    t = applied["totals"]
    assert t["insert:user_message_sent"] == 1
    assert t["events_inserted"] == 1
    assert t["field:input_tokens"] == 1
    assert t["meta_keys_added"] == 2

    with get_session() as s:
        sent = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "user_message_sent"
        )).scalars().one()
        assert sent.payload["images"][0]["data"] == _PNG_B64
        assert sent.stream_id == want_stream  # borrowed from its own turn
        assert s.get(Event, comp_id).payload["input_tokens"] == 40
        sm = s.get(Thread, tid).source_metadata
        assert sm["cli_version"] == "1.2.3" and sm["git"]["branch"] == "main"

    again = run(apply=True, items=list(iter_codex_items(sessions_dir=root)))
    assert again["totals"].get("inserts", 0) == 0
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"].get("meta_keys_missing", 0) == 0


# ── grok (session dir + summary.json) ────────────────────────────────────────

GROK_LINES = [
    {"type": "user", "content": [{"type": "text", "text": "<user_query>hi grok</user_query>"}]},
    {"type": "assistant", "content": "hello from grok", "tool_calls": [],
     "model_fingerprint": "fp_a39489019fa99b6e"},
]

GROK_SUMMARY = {
    "info": {"id": "sess-1", "cwd": "/proj"},
    "current_model_id": "grok-4", "created_at": "2026-01-01T10:00:00Z",
    "reasoning_effort": "high", "session_kind": "cli", "head_branch": "main",
}


def _make_grok_session(root, source_id="sess-1"):
    d = root / source_id
    d.mkdir(parents=True)
    f = d / "chat_history.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in GROK_LINES) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(GROK_SUMMARY), encoding="utf-8")
    return f


def test_iter_grok_items_skips_an_absent_sessions_dir(archive_home) -> None:
    assert list(iter_grok_items(sessions_dir=archive_home / "nope")) == []


def test_grok_annotations_and_session_fields_restored(archive_home) -> None:
    """The grok adapter re-parses a session dir (chat history + summary.json): the
    dropped model_fingerprint annotation and the session-level summary fields come
    back, and the events keep their identity."""
    init_db()
    root = archive_home / "grok-sessions"
    f = _make_grok_session(root)
    tid = import_grok_session_incremental(f, "sess-1").thread_id

    eid, payload, key = _event(tid, "api_request_completed")
    _set_payload(eid, {k: v for k, v in payload.items() if k != "annotations"})
    with get_session() as s:
        thread = s.get(Thread, tid)
        thread.source_metadata = {k: v for k, v in (thread.source_metadata or {}).items()
                                  if k not in ("reasoning_effort", "session_kind")}
        s.commit()

    items = list(iter_grok_items(sessions_dir=root))
    assert [(i.source, i.source_id) for i in items] == [("grok", "sess-1")]

    applied = run(apply=True, items=items)
    t = applied["totals"]
    assert t["field:annotations"] == 1
    assert t["meta_keys_added"] == 2

    with get_session() as s:
        ev = s.get(Event, eid)
        assert ev.payload["annotations"]["model_fingerprint"] == "fp_a39489019fa99b6e"
        assert ev.dedup_key == key  # identity untouched
        sm = s.get(Thread, tid).source_metadata
        assert sm["reasoning_effort"] == "high" and sm["session_kind"] == "cli"

    again = run(apply=True, items=list(iter_grok_items(sessions_dir=root)))
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"].get("meta_keys_missing", 0) == 0


# ── antigravity / cowork discovery guards ────────────────────────────────────

def test_iter_antigravity_items_skips_an_absent_brain_dir(archive_home) -> None:
    assert list(iter_antigravity_items(brain_dir=archive_home / "nope")) == []


COWORK_LINES = [
    {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
     "sessionId": "s1", "cwd": "/proj",
     "message": {"role": "user", "content": "cowork task"}},
    {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
     "message": {"role": "assistant", "model": "claude-opus-4",
                 "usage": {"input_tokens": 9, "output_tokens": 3},
                 "content": [{"type": "text", "text": "on it"}]}},
    {"type": "result", "_audit_timestamp": "2026-01-01T10:00:06Z",
     "total_cost_usd": 0.12, "num_turns": 2},
]


def _make_cowork_session(session="local_sess1"):
    # Place the session where the product's own resolver looks on THIS host, so
    # discovery finds it on macOS (~/Library/Application Support) and Linux
    # (~/.config) alike — driving the real path instead of patching platform.
    # HOME is already pointed at the fake machine by the caller.
    from thread_archive._watcher.paths import app_data_dir

    base = app_data_dir()
    assert base is not None, "app_data_dir() has no root on this platform"
    d = base / "Claude" / "local-agent-mode-sessions" / "user1" / "org1" / session
    d.mkdir(parents=True)
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(x) for x in COWORK_LINES) + "\n",
                     encoding="utf-8")
    return audit


def test_iter_cowork_items_is_empty_without_session_dirs(archive_home) -> None:
    assert list(iter_cowork_items()) == []  # no cowork store on this (sandbox) machine


def test_cowork_session_stats_and_token_placeholder_restored(
    archive_home, tmp_path, monkeypatch
) -> None:
    """The cowork adapter re-parses an ``audit.jsonl`` through the Claude Code
    builders: the newest ``result`` line's aggregates return to the thread and the
    zero-token placeholder is replaced by the real count."""
    init_db()
    fake_home = tmp_path / "cowork-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    audit = _make_cowork_session()
    tid = import_cowork_session_incremental(audit, "user1:org1:sess1", None).thread_id

    eid, payload, _ = _event(tid, "api_request_completed")
    _set_payload(eid, {**payload, "input_tokens": 0})
    with get_session() as s:
        thread = s.get(Thread, tid)
        thread.source_metadata = {k: v for k, v in (thread.source_metadata or {}).items()
                                  if k != "session_stats"}
        s.commit()

    items = list(iter_cowork_items())
    assert [(i.source, i.source_id) for i in items] == [("cowork", "user1:org1:sess1")]

    applied = run(apply=True, items=items)
    assert applied["totals"]["field:input_tokens"] == 1
    assert applied["totals"]["meta_keys_added"] == 1

    with get_session() as s:
        assert s.get(Event, eid).payload["input_tokens"] == 9
        stats = s.get(Thread, tid).source_metadata["session_stats"]
        assert stats == {"total_cost_usd": 0.12, "num_turns": 2}

    again = run(apply=True, items=list(iter_cowork_items()))
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"].get("meta_keys_missing", 0) == 0


# ── claude-science (per-org frame DB) ────────────────────────────────────────

SCIENCE_MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "what can i do here?"}],
     "_uuid": "u1", "_rolling_summary": "the story so far"},
    # A store row that isn't a conversation message at all — skipped by both the
    # importer and the re-parse, so the synthetic timestamps still line up.
    {"role": "system", "content": [{"type": "text", "text": "bookkeeping"}], "_uuid": "sys1"},
    {"role": "assistant", "content": [{"type": "text", "text": "lots of science"}],
     "_uuid": "a1", "_response_id": "resp1", "_artifact_refs": ["art-1"],
     "_tokens": {"input": 11, "output": 7, "cache_read": 100}},
    # A message carrying no science-only extras — nothing to merge onto it.
    {"role": "user", "content": [{"type": "text", "text": "thanks!"}], "_uuid": "u2"},
]


def _science_user_event(tid, content):
    """The user event of a science thread whose payload carries ``content``."""
    with get_session() as s:
        rows = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "user_message_sent"
        )).scalars().all()
        [ev] = [e for e in rows if e.payload.get("content") == content]
        return ev.id, dict(ev.payload), ev.dedup_key


def _make_science_db(base, org="org-1"):
    d = base / org
    d.mkdir(parents=True)
    db = d / "operon-cli.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE frames (id TEXT PRIMARY KEY, parent_frame_id TEXT, "
        "root_frame_id TEXT, agent_name TEXT, status TEXT, conversation_type TEXT, "
        "name TEXT, task_summary TEXT, model TEXT, project_id TEXT, "
        "created_at INTEGER, updated_at INTEGER, total_cost REAL, effort TEXT)"
    )
    conn.execute(
        "CREATE TABLE frame_messages (frame_id TEXT, idx INTEGER, msg_json TEXT, "
        "PRIMARY KEY(frame_id, idx))"
    )
    conn.execute(
        "INSERT INTO frames (id, agent_name, status, conversation_type, name, model, "
        "project_id, created_at, total_cost, effort) VALUES "
        "('root1','OPERON','completed','agent','Explore','claude-opus-4-8','proj_real',"
        "1700000000000, 1.25, 'high')"
    )
    rows = [("root1", i, json.dumps(m)) for i, m in enumerate(SCIENCE_MESSAGES)]
    rows.append(("root1", len(rows), "{not json"))       # corrupt row
    rows.append(("root1", len(rows) + 1, json.dumps("just a string")))  # not a message
    conn.executemany("INSERT INTO frame_messages VALUES (?,?,?)", rows)
    # A frame with no messages at all — nothing to re-derive, skipped.
    conn.execute(
        "INSERT INTO frames (id, agent_name, conversation_type, name, project_id, "
        "created_at) VALUES ('empty1','OPERON','agent','Empty','proj_real',1700000001000)"
    )
    conn.commit()
    conn.close()
    return db


def test_iter_claude_science_items_skips_schemaless_orgs(archive_home) -> None:
    """An org DB without a ``frames`` table (a store the app hasn't populated) is
    passed over, not treated as an error."""
    base = archive_home / "science"
    (base / "org-empty").mkdir(parents=True)
    sqlite3.connect(base / "org-empty" / "operon-cli.db").close()
    assert list(iter_claude_science_items(base=base)) == []


def test_claude_science_annotations_and_frame_stats_restored(archive_home) -> None:
    """The claude-science adapter re-synthesizes CC-shaped lines from a frame,
    re-merges the science-only message annotations, and restores the frame's
    aggregate stats — skipping the frame with no messages and the unparseable rows."""
    init_db()
    base = archive_home / "science"
    db = _make_science_db(base)
    assert import_claude_science_db(db, "org-1").imported == 1
    with get_session() as s:
        tid = s.execute(select(Thread.id).where(
            Thread.source == "claude-science")).scalar_one()

    # OLD shape: the message annotations were dropped on both the user anchor and
    # the assistant anchor, and the frame stat columns never reached the thread.
    user_id, user_payload, user_key = _science_user_event(tid, "what can i do here?")
    _set_payload(user_id, {k: v for k, v in user_payload.items() if k != "annotations"})
    comp_id, comp_payload, _ = _event(tid, "api_request_completed")
    _set_payload(comp_id, {k: v for k, v in comp_payload.items() if k != "annotations"})
    with get_session() as s:
        thread = s.get(Thread, tid)
        thread.source_metadata = {k: v for k, v in (thread.source_metadata or {}).items()
                                  if k not in ("total_cost", "effort")}
        s.commit()

    items = list(iter_claude_science_items(base=base))
    assert [(i.source, i.source_id) for i in items] == [("claude-science", "org-1:root1")]

    applied = run(apply=True, items=items)
    t = applied["totals"]
    assert t["field:annotations"] == 2
    assert t["meta_keys_added"] == 2

    with get_session() as s:
        user_ev = s.get(Event, user_id)
        assert user_ev.payload["annotations"]["rolling_summary"] == "the story so far"
        assert user_ev.dedup_key == user_key  # identity untouched
        ann = s.get(Event, comp_id).payload["annotations"]
        assert ann["artifact_refs"] == ["art-1"] and ann["message_id"] == "resp1"
        sm = s.get(Thread, tid).source_metadata
        assert sm["total_cost"] == 1.25 and sm["effort"] == "high"

    again = run(apply=True, items=list(iter_claude_science_items(base=base)))
    assert again["totals"].get("patches", 0) == 0
    assert again["totals"].get("meta_keys_missing", 0) == 0


def test_module_is_runnable_as_a_script(archive_home) -> None:
    """``python -m`` is how an operator runs this — a real child process, parsing
    its own argv and dispatching to ``main``. Restricted to one source, pointed at
    an empty ``$HOME`` so its store is absent and the sweep is a dry no-op."""
    init_db()
    empty_home = archive_home / "_no-tools-home"
    empty_home.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", bdf.__name__, "--source", "codex"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(empty_home),
             "THREAD_ARCHIVE_HOME": str(archive_home)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "[DRY-RUN] backfill-dropped-fields" in proc.stdout
    assert "threads examined:    0" in proc.stdout
