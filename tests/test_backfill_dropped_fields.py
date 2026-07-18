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
import sqlite3

import pytest
from sqlalchemy import delete, select, update

from thread_archive._importers import (
    import_antigravity_session_incremental,
    import_cursor_db,
    import_opencode_db,
    import_session_incremental,
)
from thread_archive._importers._read import read_session_lines
from thread_archive._ops.amend import load_amendments
from thread_archive._scripts.backfill_dropped_fields import (
    WorkItem,
    iter_antigravity_items,
    iter_cursor_items,
    iter_opencode_items,
    main,
    run,
)
from thread_archive._scripts.backfill_reconcile import _fresh_events as cc_fresh
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._thread_import.event_builder import compute_dedup_key

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
def demo_harness():
    """A registered provider whose harness writes Claude-Code-shaped JSONL.

    The audit derives its Claude-Code-shaped adapters from the registry, so a
    source is re-parsed exactly when a provider declares that parser. Registering
    one is what makes the audit reach a source outside the built-in set — a
    source with no adapter isn't audited, and nothing reports that it wasn't.
    """
    from thread_archive import _providers
    from thread_archive.provider import Provider, claude_code_line_stream
    from thread_archive.provider.parse import CLAUDE_CODE_CONFIG, register_provider_config

    provider = Provider(
        name="demo-harness", label="Demo",
        parser_id="claude-code",
        parser_config=CLAUDE_CODE_CONFIG.derive("demo-harness"),
        kind="line-stream",
        importer=claude_code_line_stream("demo-harness"),
    )
    builtin = dict(_providers.registry())
    register_provider_config(provider.parser_config)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_providers, "registry",
                   lambda *a, **kw: {**builtin, provider.name: provider})
        yield provider
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
