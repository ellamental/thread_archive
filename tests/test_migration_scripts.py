"""The migration/repair scripts' full run paths: dry-run plans without mutating,
apply matches the plan and writes a backup first, and a second apply is a no-op.

The plan-level logic of ``backfill_recompute`` / ``denamespace_dedup_keys`` /
``recover_dropped_events`` is covered in their sibling test modules; this module
drives the ``run()`` / ``main()`` orchestration those tests bypass — the code that
actually touches the live store when a migration is executed — plus the whole of
``repair_grok_tool_names``, driven by a synthetic plan of the production shape
(the real plan/backup dumps are operator data holding private conversation
payloads — they live untracked in ``host/repair-dumps/``, never in the repo,
so no test may depend on them existing).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select, update

from thread_archive._importers import import_session_incremental
from thread_archive._store import Event, Thread, get_session, init_db

LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "s1",
     "cwd": "/p", "message": {"role": "user", "content": "hello there"}},
    {"type": "user", "uuid": "u_ide", "timestamp": "2026-01-01T10:00:01Z", "sessionId": "s1",
     "message": {"role": "user", "content": "<ide_selection>def foo(): pass</ide_selection>"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T10:00:05Z",
     "sessionId": "s1", "message": {"role": "assistant", "model": "m", "content": [
         {"type": "thinking", "thinking": "hmm"},
         {"type": "text", "text": "hi back"},
         {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-01-01T10:00:06Z",
     "sessionId": "s1", "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu1", "content": "file.txt"}]}},
]


def _import_thread(archive_home) -> int:
    init_db()
    f = archive_home / "sess.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    return import_session_incremental(f, "proj:s1").thread_id, f


def _keys(tid: int) -> dict[int, str | None]:
    with get_session() as s:
        evs = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()
        return {e.id: e.dedup_key for e in evs}


# ── denamespace_dedup_keys: run() / main() ───────────────────────────────────

def _prefix_all_keys(tid: int) -> dict[int, str]:
    with get_session() as s:
        evs = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().all()
        bare = {e.id: e.dedup_key for e in evs}
        for e in evs:
            s.execute(update(Event).where(Event.id == e.id).values(dedup_key=f"{tid}:{e.dedup_key}"))
        s.commit()
    return bare


def test_denamespace_dry_run_mutates_nothing(archive_home) -> None:
    from thread_archive._scripts.denamespace_dedup_keys import run

    tid, _ = _import_thread(archive_home)
    bare = _prefix_all_keys(tid)

    totals = run(apply=False)
    assert totals["threads"] == 1
    assert totals["stripped"] == len(bare)
    # dry-run: every key still carries the legacy prefix
    assert all(k == f"{tid}:{bare[eid]}" for eid, k in _keys(tid).items() if k)


def test_denamespace_apply_writes_backup_then_strips_and_is_idempotent(archive_home, tmp_path) -> None:
    from thread_archive._scripts.denamespace_dedup_keys import run

    tid, _ = _import_thread(archive_home)
    bare = _prefix_all_keys(tid)
    backup = tmp_path / "denamespace-backup.jsonl"

    totals = run(apply=True, backup_path=backup)
    assert totals["stripped"] == len(bare)
    # keys are back to exactly the current bare form
    assert {eid: k for eid, k in _keys(tid).items() if k} == bare
    # the backup holds every original (prefixed) key, reversibly
    rows = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert rows and rows[0]["thread_id"] == tid
    backed_up = {r["id"]: r["old"] for r in rows[0]["rows"]}
    assert backed_up == {eid: f"{tid}:{k}" for eid, k in bare.items()}

    # second apply: nothing left to strip
    totals2 = run(apply=True, backup_path=backup)
    assert totals2.get("stripped", 0) == 0
    assert totals2.get("threads", 0) == 0


def test_denamespace_main_reports_mode(archive_home, capsys) -> None:
    from thread_archive._scripts.denamespace_dedup_keys import main

    tid, _ = _import_thread(archive_home)
    _prefix_all_keys(tid)

    main([])
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out

    main(["--apply"])
    out = capsys.readouterr().out
    assert "[APPLIED]" in out
    assert not any((k or "").startswith(f"{tid}:") for k in _keys(tid).values())


# ── backfill_reconcile: plan_thread drift guard + run() ──────────────────────

def test_reconcile_plan_backfills_nulled_keys_exactly(archive_home) -> None:
    from thread_archive._scripts.backfill_reconcile import plan_thread

    tid, _ = _import_thread(archive_home)
    original = {eid: k for eid, k in _keys(tid).items() if k}
    with get_session() as s:
        s.execute(update(Event).where(Event.thread_id == tid).values(dedup_key=None))
        s.commit()

    with get_session() as s:
        plan = plan_thread(s, tid, "claude-code", LINES)
    assert plan.safe
    assert plan.backfills
    for eid, key in plan.backfills:
        assert key == original[eid], "backfilled key must equal what the builder wrote"


def test_reconcile_plan_flags_key_drift_as_unsafe(archive_home) -> None:
    from thread_archive._scripts.backfill_reconcile import plan_thread

    tid, _ = _import_thread(archive_home)
    # same content, but the stored key genuinely differs from a fresh build → drift
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().first()
        s.execute(update(Event).where(Event.id == ev.id).values(dedup_key="drifted-key"))
        s.commit()

    with get_session() as s:
        plan = plan_thread(s, tid, "claude-code", LINES)
    assert not plan.safe
    assert any("drift" in w for w in plan.warnings)


def test_reconcile_run_dry_apply_backup_idempotent(archive_home, tmp_path, monkeypatch) -> None:
    from thread_archive._scripts import backfill_reconcile as mod

    tid, f = _import_thread(archive_home)
    with get_session() as s:
        s.execute(update(Event).where(Event.thread_id == tid).values(dedup_key=None))
        s.commit()

    pairs = [("claude-code", f, "proj:s1")]

    totals = mod.run(apply=False, pairs=pairs)
    assert totals["threads"] == 1
    assert totals["backfills"] > 0
    assert all(k is None for k in _keys(tid).values()), "dry-run must not write"

    backup = tmp_path / "reconcile-backup.jsonl"
    totals = mod.run(apply=True, backup_path=backup, pairs=pairs)
    assert totals["threads_changed"] == 1
    keyed = {eid: k for eid, k in _keys(tid).items() if k}
    assert len(keyed) == totals["backfills"]
    rows = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert rows[0]["thread_id"] == tid
    assert set(rows[0]["backfill_ids"]) == set(keyed)

    totals2 = mod.run(apply=True, backup_path=backup, pairs=pairs)
    assert totals2["backfills"] == 0, "second apply must be a no-op"


# ── recover_dropped_events: run() ────────────────────────────────────────────

def test_recover_run_dry_apply_idempotent(archive_home, monkeypatch) -> None:
    from thread_archive._scripts import recover_dropped_events as mod

    tid, f = _import_thread(archive_home)
    with get_session() as s:
        n_full = len(s.execute(select(Event).where(Event.thread_id == tid)).scalars().all())
        from sqlalchemy import delete
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type.in_(list(mod.RECOVERABLE_TYPES))))
        s.commit()

    pairs = [("claude-code", f, "proj:s1")]

    totals = mod.run(apply=False, pairs=pairs)
    assert totals["threads_mapped"] == 1
    assert totals["events_recovered"] > 0
    with get_session() as s:
        assert len(s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()) < n_full

    totals = mod.run(apply=True, pairs=pairs)
    assert totals["write_errors"] == 0
    with get_session() as s:
        assert len(s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()) == n_full

    totals = mod.run(apply=True, pairs=pairs)
    assert totals["events_recovered"] == 0, "second apply must be a no-op"


# ── repair_grok_tool_names: a synthetic plan of the production shape ─────────

def _grok_plan_row(event_id: int) -> dict:
    """One patch row shaped like the production plan: a tool_execution_completed
    whose live tail-import recorded ``tool_name: "unknown"`` at a stale timestamp."""
    old_payload = {"tool_call_id": f"call-{event_id}", "tool_name": "unknown",
                   "output": f"output {event_id}", "is_error": False}
    new_payload = {**old_payload, "tool_name": "Read"}
    canon = lambda o: json.dumps(o, sort_keys=True, separators=(",", ":"))  # noqa: E731
    return {
        "event_id": event_id,
        "old_event_type": "tool_execution_completed",
        "new_event_type": "tool_execution_completed",
        "old_occurred_at": "2026-06-29 23:00:00.000000",
        "new_occurred_at": "2026-06-29 23:06:35.000000",
        "old_payload": canon(old_payload),
        "new_payload": canon(new_payload),
    }


def _write_grok_plan(tmp_path, monkeypatch, patches: list[dict]):
    from thread_archive._scripts import repair_grok_tool_names as mod

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"pg": [], "sa": patches}))
    monkeypatch.setattr(mod, "PLAN_PATH", plan)
    return mod


@pytest.fixture
def grok_plan_seeded(archive_home, tmp_path, monkeypatch):
    """Seed a store holding exactly the events a synthetic repair plan targets,
    with the plan's asserted old values."""
    from thread_archive._retrieval.fts import ensure_fts

    init_db()
    ensure_fts()  # the script raw-DELETEs from event_search; a real store has it
    patches = [_grok_plan_row(eid) for eid in (8225001, 8225002, 8225003)]
    mod = _write_grok_plan(tmp_path, monkeypatch, patches)
    with get_session() as s:
        s.add(Thread(id=1, name="grok-repair-fixture", source="grok"))
        s.flush()
        for p in patches:
            s.add(Event(
                id=p["event_id"], thread_id=1, stream_id="st",
                event_type=p["old_event_type"],
                payload=json.loads(p["old_payload"]),
                occurred_at=datetime.fromisoformat(p["old_occurred_at"]),
            ))
        s.commit()
    return mod, patches


def test_grok_repair_preview_only_writes_nothing(grok_plan_seeded, monkeypatch, tmp_path, capsys) -> None:
    mod, patches = grok_plan_seeded
    monkeypatch.setattr(mod, "BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("sys.argv", ["repair_grok_tool_names.py"])

    assert mod.main() == 0
    assert "preview only" in capsys.readouterr().out
    assert not (tmp_path / "backup.json").exists()
    with get_session() as s:
        for p in patches:
            ev = s.get(Event, p["event_id"])
            assert mod.canonical_json(ev.payload) == p["old_payload"]


def test_grok_repair_apply_backs_up_then_patches(grok_plan_seeded, monkeypatch, tmp_path) -> None:
    mod, patches = grok_plan_seeded
    backup = tmp_path / "backup.json"
    monkeypatch.setattr(mod, "BACKUP_PATH", backup)
    monkeypatch.setattr("sys.argv", ["repair_grok_tool_names.py", "--apply"])

    assert mod.main() == 0
    # backup captured the pre-repair rows
    backed_up = {r["id"] for r in json.loads(backup.read_text())}
    assert backed_up == {p["event_id"] for p in patches}
    # every event now carries the plan's new values
    with get_session() as s:
        for p in patches:
            ev = s.get(Event, p["event_id"])
            assert mod.canonical_json(ev.payload) == p["new_payload"]
            assert ev.event_type == p["new_event_type"]
            assert ev.occurred_at == datetime.fromisoformat(p["new_occurred_at"])
    # the corrected rows were appended to the thread's truth file as history
    from thread_archive._config import resolve_paths

    truth_file = resolve_paths(None).truth_dir / "threads" / "1.jsonl"
    assert truth_file.exists()
    lines = [json.loads(ln) for ln in truth_file.read_text().splitlines()]
    patched_ids = {p["event_id"] for p in patches}
    assert {ln["id"] for ln in lines if ln.get("type") == "event"} >= patched_ids


def test_grok_repair_aborts_on_missing_event(archive_home, tmp_path, monkeypatch, capsys) -> None:
    mod = _write_grok_plan(tmp_path, monkeypatch, [_grok_plan_row(8225001)])

    init_db()  # empty store: first planned event is absent
    monkeypatch.setattr("sys.argv", ["repair_grok_tool_names.py", "--apply"])
    assert mod.main() == 1
    assert "ABORT" in capsys.readouterr().out


def test_grok_repair_aborts_on_payload_mismatch(grok_plan_seeded, monkeypatch, tmp_path, capsys) -> None:
    mod, patches = grok_plan_seeded
    monkeypatch.setattr(mod, "BACKUP_PATH", tmp_path / "backup.json")
    with get_session() as s:
        ev = s.get(Event, patches[0]["event_id"])
        ev.payload = {"tampered": True}
        s.commit()

    monkeypatch.setattr("sys.argv", ["repair_grok_tool_names.py", "--apply"])
    assert mod.main() == 1
    assert "no longer matches plan" in capsys.readouterr().out
    # nothing was written: the other events still hold their old payloads
    with get_session() as s:
        for p in patches[1:]:
            assert mod.canonical_json(s.get(Event, p["event_id"]).payload) == p["old_payload"]
    assert not (tmp_path / "backup.json").exists()
