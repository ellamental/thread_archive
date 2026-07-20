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
from datetime import datetime, timezone
from pathlib import Path

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


def test_denamespace_thread_listing_respects_limit(archive_home) -> None:
    from thread_archive._scripts.denamespace_dedup_keys import _prefixed_thread_ids

    tid, _ = _import_thread(archive_home)
    _prefix_all_keys(tid)
    with get_session() as s:
        other = Thread(name="second-prefixed-thread", source="claude-code")
        s.add(other)
        s.flush()
        s.add(Event(thread_id=other.id, stream_id="st", event_type="text_complete",
                    payload={"block_index": 0, "text": "x"},
                    occurred_at=datetime(2026, 1, 1, 10, 0, 0),
                    dedup_key=f"{other.id}:c=abc:text_complete:blk=0:abc"))
        s.commit()

    with get_session() as s:
        assert len(_prefixed_thread_ids(s, None)) == 2
        assert len(_prefixed_thread_ids(s, 1)) == 1


def test_denamespace_leaves_already_bare_keys_alone(archive_home) -> None:
    """The two formats coexist mid-migration; only the prefixed rows are rewritten."""
    from thread_archive._scripts.denamespace_dedup_keys import run

    tid, _ = _import_thread(archive_home)
    with get_session() as s:
        evs = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().all()
        bare = {e.id: e.dedup_key for e in evs}
        prefixed = sorted(bare)[:2]
        for eid in prefixed:
            s.execute(update(Event).where(Event.id == eid).values(dedup_key=f"{tid}:{bare[eid]}"))
        s.commit()

    totals = run(apply=True)
    assert totals["stripped"] == len(prefixed)
    assert {eid: k for eid, k in _keys(tid).items() if k} == bare


def test_denamespace_counts_a_collapse_onto_an_existing_bare_key(archive_home) -> None:
    """A prefixed key whose bare form is already present is the same event identity
    stored twice — de-prefixing still collapses them to one, and the pass says so."""
    from thread_archive._scripts.denamespace_dedup_keys import run

    tid, _ = _import_thread(archive_home)
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().first()
        s.add(Event(thread_id=tid, stream_id="twin-stream", api_call_id="twin-call",
                    event_type=ev.event_type, payload=dict(ev.payload),
                    occurred_at=ev.occurred_at, dedup_key=f"{tid}:{ev.dedup_key}"))
        s.commit()

    totals = run(apply=False)
    assert totals["stripped"] == 1
    assert totals["collapses"] == 1


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


def _write_grok_plan(tmp_path, patches: list[dict]):
    """Write a synthetic plan and hand back ``(module, plan path)``."""
    from thread_archive._scripts import repair_grok_tool_names as mod

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"pg": [], "sa": patches}))
    return mod, plan


def _seed_grok_events(patches: list[dict]) -> None:
    """A store holding exactly the events a repair plan targets, at its old values."""
    from thread_archive._retrieval.fts import ensure_fts

    init_db()
    ensure_fts()  # the script raw-DELETEs from event_search; a real store has it
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


@pytest.fixture
def grok_plan_seeded(archive_home, tmp_path):
    """Seed a store holding exactly the events a synthetic repair plan targets,
    with the plan's asserted old values. Yields ``(module, plan path, patches)``."""
    patches = [_grok_plan_row(eid) for eid in (8225001, 8225002, 8225003)]
    mod, plan = _write_grok_plan(tmp_path, patches)
    _seed_grok_events(patches)
    return mod, plan, patches


def test_grok_backup_path_is_stamped_when_it_is_written() -> None:
    """The undo dump's name comes from the moment of the write, not from whenever
    the module happened to be imported — two runs must not collide on one file."""
    from thread_archive._scripts import repair_grok_tool_names as mod

    plan = Path("/plans/repair_grok_tool_names_plan_20260704.json")
    first = mod.backup_path_for(plan, now=datetime(2026, 7, 4, 1, 2, 3, tzinfo=timezone.utc))
    second = mod.backup_path_for(plan, now=datetime(2026, 7, 4, 4, 5, 6, tzinfo=timezone.utc))
    assert first.parent == plan.parent  # the dump lands beside the plan it read
    assert first.name == "repair_grok_tool_names_backup_20260704T010203Z.json"
    assert second.name == "repair_grok_tool_names_backup_20260704T040506Z.json"


def test_grok_repair_preview_only_writes_nothing(grok_plan_seeded, tmp_path, capsys) -> None:
    mod, plan, patches = grok_plan_seeded

    assert mod.main(["--plan", str(plan)]) == 0
    assert "preview only" in capsys.readouterr().out
    assert not list(tmp_path.glob("repair_grok_tool_names_backup_*.json"))
    with get_session() as s:
        for p in patches:
            ev = s.get(Event, p["event_id"])
            assert mod.canonical_json(ev.payload) == p["old_payload"]


def test_grok_repair_apply_backs_up_then_patches(grok_plan_seeded, tmp_path) -> None:
    mod, plan, patches = grok_plan_seeded

    # No --backup: the run resolves its own dump path, beside the plan, at write time.
    assert mod.main(["--apply", "--plan", str(plan)]) == 0
    (backup,) = tmp_path.glob("repair_grok_tool_names_backup_*.json")
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


def test_grok_repair_aborts_on_missing_event(archive_home, tmp_path, capsys) -> None:
    mod, plan = _write_grok_plan(tmp_path, [_grok_plan_row(8225001)])

    init_db()  # empty store: first planned event is absent
    assert mod.main(["--apply", "--plan", str(plan)]) == 1
    assert "ABORT" in capsys.readouterr().out


def test_grok_repair_retypes_and_rekeys(archive_home, tmp_path, capsys) -> None:
    """A plan row may move the event to a different type and name the dedup_key that
    type change implies; both land, and the preview line names the retype."""
    patch = _grok_plan_row(8225010)
    patch["new_event_type"] = "tool_execution_error"
    patch["new_dedup_key"] = "c=0123456789abcdef:tool_execution_error::0123456789abcdef"
    # a pure retype: the timestamp and payload the plan asserts are the ones it keeps
    patch["new_occurred_at"] = patch["old_occurred_at"]
    patch["new_payload"] = patch["old_payload"]
    mod, plan = _write_grok_plan(tmp_path, [patch])
    _seed_grok_events([patch])

    assert mod.main(
        ["--apply", "--plan", str(plan), "--backup", str(tmp_path / "backup.json")]
    ) == 0
    out = capsys.readouterr().out
    assert "type tool_execution_completed -> tool_execution_error" in out
    with get_session() as s:
        ev = s.get(Event, patch["event_id"])
        assert ev.event_type == "tool_execution_error"
        assert ev.dedup_key == patch["new_dedup_key"]


def test_grok_repair_aborts_on_event_type_mismatch(grok_plan_seeded, tmp_path, capsys) -> None:
    """The plan asserts the type it expects to find as well as the payload — a row
    that has moved type since the plan was cut is not the row it describes."""
    mod, plan, patches = grok_plan_seeded
    with get_session() as s:
        s.execute(update(Event).where(Event.id == patches[0]["event_id"]).values(
            event_type="text_complete"))
        s.commit()

    assert mod.main(
        ["--apply", "--plan", str(plan), "--backup", str(tmp_path / "backup.json")]
    ) == 1
    assert "!= plan" in capsys.readouterr().out
    assert not (tmp_path / "backup.json").exists()
    with get_session() as s:
        for p in patches[1:]:
            assert mod.canonical_json(s.get(Event, p["event_id"]).payload) == p["old_payload"]


def test_grok_repair_aborts_on_payload_mismatch(grok_plan_seeded, tmp_path, capsys) -> None:
    mod, plan, patches = grok_plan_seeded
    with get_session() as s:
        ev = s.get(Event, patches[0]["event_id"])
        ev.payload = {"tampered": True}
        s.commit()

    assert mod.main(
        ["--apply", "--plan", str(plan), "--backup", str(tmp_path / "backup.json")]
    ) == 1
    assert "no longer matches plan" in capsys.readouterr().out
    # nothing was written: the other events still hold their old payloads
    with get_session() as s:
        for p in patches[1:]:
            assert mod.canonical_json(s.get(Event, p["event_id"]).payload) == p["old_payload"]
    assert not (tmp_path / "backup.json").exists()
