"""Orchestration + edge-branch coverage for the migration/backfill scripts.

The plan-level happy paths of these four scripts are covered by their sibling
modules (``test_backfill_dedup_key`` / ``test_backfill_codex_model`` /
``test_recover_dropped_events`` / ``test_migration_scripts``). This module drives
the parts those bypass: the ``main`` CLI entrypoints, the ``run`` error/skip
branches (plan errors, read/write errors, unmatched sources, limit caps), the pure
anchor/normalize helpers, and the collision/collapse arms of ``backfill_recompute``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text as sa_text, update

from thread_archive._importers import (
    import_codex_session_incremental,
    import_session_incremental,
)
from thread_archive._importers import codex as codex_mod
from thread_archive._store import Event, ImportState, Thread, get_session, init_db
from thread_archive._thread_import.event_builder import compute_dedup_key

# ── shared CC fixture ────────────────────────────────────────────────────────

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


def _import_cc(archive_home, source_id="proj:s1"):
    init_db()
    f = archive_home / "sess.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in LINES) + "\n", encoding="utf-8")
    return import_session_incremental(f, source_id).thread_id, f


def _keys(tid: int) -> dict[int, str | None]:
    with get_session() as s:
        evs = s.execute(select(Event).where(Event.thread_id == tid)).scalars().all()
        return {e.id: e.dedup_key for e in evs}


def _null_all_keys(tid: int) -> None:
    with get_session() as s:
        s.execute(update(Event).where(Event.thread_id == tid).values(dedup_key=None))
        s.commit()


class FakeWatcher:
    """A watcher whose availability and file listing are fixed for a test."""

    def __init__(self, available: bool, files=()) -> None:
        self._available = available
        self._files = list(files)

    def is_available(self) -> bool:
        return self._available

    def _iter_files(self):
        yield from self._files


# ══ backfill_recompute ═══════════════════════════════════════════════════════

from thread_archive._scripts import backfill_recompute as rc  # noqa: E402


def _seed_recompute_thread(archive_home, events, *, source_id="man:1") -> int:
    """Seed a thread + import_state + hand-built events; returns the thread id.

    Each event dict carries stream_id / api_call_id / event_type / payload /
    dedup_key (and optional occurred_at)."""
    init_db()
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    with get_session() as s:
        t = Thread(name=f"recompute-fixture-{source_id}", source="claude-code")
        s.add(t)
        s.flush()
        tid = t.id
        s.add(ImportState(source="claude-code", source_id=source_id, thread_id=tid))
        for i, e in enumerate(events):
            s.add(Event(
                thread_id=tid,
                stream_id=e["stream_id"],
                api_call_id=e.get("api_call_id"),
                event_type=e["event_type"],
                payload=e["payload"],
                occurred_at=e.get("occurred_at", base),
                dedup_key=e.get("dedup_key"),
            ))
        s.commit()
    return tid


def _cite(tid: int, event_ids: list[int]) -> None:
    with get_session() as s:
        for eid in event_ids:
            s.execute(sa_text(
                "INSERT INTO topic_messages (topic_id, event_id, thread_id, quote) "
                "VALUES (:t, :e, :t, 'q')"
            ), {"t": tid, "e": eid})
        s.commit()


def test_recompute_main_dry_run_then_apply(archive_home, capsys) -> None:
    tid, _ = _import_cc(archive_home)
    original = {eid: k for eid, k in _keys(tid).items() if k}
    _null_all_keys(tid)

    rc.main([])
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "dedup_key backfills" in out
    assert all(k is None for k in _keys(tid).values()), "dry-run must not write"

    rc.main(["--apply", "-v"])
    out = capsys.readouterr().out
    assert "[APPLIED]" in out
    keyed = {eid: k for eid, k in _keys(tid).items() if k}
    # every recovered key equals the builder's original for the whitelisted types
    assert keyed
    for eid, k in keyed.items():
        assert k == original[eid]


def test_recompute_main_collapse_reports_and_backs_up(archive_home, tmp_path, capsys) -> None:
    tid, f = _import_cc(archive_home)
    # lost-watermark shape: null keys, rewind the cursor, re-import → every turn dupes.
    _null_all_keys(tid)
    with get_session() as s:
        s.execute(sa_text("UPDATE import_state SET last_line_count = 0, last_file_size = 0"))
        s.commit()
    import_session_incremental(f, "proj:s1")

    backup = tmp_path / "collapse-backup.jsonl"
    rc.main(["--apply", "--collapse", "--backup", str(backup)])
    out = capsys.readouterr().out
    assert "[APPLIED]" in out
    assert "duplicate pairs collapsed" in out
    rows = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert rows and any(r["collapses"] for r in rows)


def test_recompute_run_counts_plan_errors(archive_home, monkeypatch) -> None:
    tid, _ = _import_cc(archive_home)
    _null_all_keys(tid)

    def _boom(session, thread_id, *, collapse=False):
        raise RuntimeError("planning blew up")

    monkeypatch.setattr(rc, "plan_thread", _boom)
    totals = rc.run(apply=False)
    assert totals["plan_errors"] == 1
    assert totals["threads"] == 1


def test_recompute_threads_with_null_keys_respects_limit(archive_home) -> None:
    init_db()
    # two import-derived threads, each with a NULL-key event
    ids = []
    for i in range(2):
        tid = _seed_recompute_thread(
            archive_home,
            [{"stream_id": f"st{i}", "api_call_id": None, "event_type": "text_complete",
              "payload": {"block_index": 0, "text": "x"}, "dedup_key": None}],
            source_id=f"man:{i}",
        )
        ids.append(tid)
    with get_session() as s:
        assert len(rc._threads_with_null_keys(s, None)) == 2
        assert len(rc._threads_with_null_keys(s, 1)) == 1


def test_recompute_multiple_anchor_pmids_warns(archive_home) -> None:
    """A group whose anchors disagree on provider_message_id can't be keyed — warned."""
    tid = _seed_recompute_thread(archive_home, [
        {"stream_id": "st", "api_call_id": "c", "event_type": "api_request_started",
         "payload": {"provider_data": {"provider_message_id": "pm1"}}, "dedup_key": "k-a1"},
        {"stream_id": "st", "api_call_id": "c", "event_type": "api_request_started",
         "payload": {"provider_data": {"provider_message_id": "pm2"}}, "dedup_key": "k-a2"},
        {"stream_id": "st", "api_call_id": "c", "event_type": "text_complete",
         "payload": {"block_index": 0, "text": "x"}, "dedup_key": None},
    ])
    totals = rc.run(apply=False)
    assert totals["threads_with_warnings"] == 1
    assert totals["warnings"] >= 1
    assert totals.get("backfills", 0) == 0


def _collision_events():
    pa = {"provider_data": {"provider_message_id": "pm1"}}
    pb = {"block_index": 0, "text": "hi"}
    b_key = compute_dedup_key("pm1", "text_complete", pb)
    return pa, pb, b_key, [
        {"stream_id": "st", "api_call_id": "c", "event_type": "api_request_started",
         "payload": pa, "dedup_key": compute_dedup_key("pm1", "api_request_started", pa)},
        {"stream_id": "st", "api_call_id": "c", "event_type": "text_complete",
         "payload": dict(pb), "dedup_key": b_key},                       # keyed original (B)
        {"stream_id": "st", "api_call_id": "c", "event_type": "text_complete",
         "payload": dict(pb), "dedup_key": None},                        # NULL twin (C)
    ]


def test_recompute_collision_leaves_null_without_collapse(archive_home) -> None:
    _pa, _pb, _bkey, events = _collision_events()
    _seed_recompute_thread(archive_home, events)
    totals = rc.run(apply=False, collapse=False)
    assert totals["collisions"] == 1
    assert totals.get("backfills", 0) == 0


def test_recompute_collapse_keeps_keyed_original(archive_home) -> None:
    _pa, _pb, b_key, events = _collision_events()
    tid = _seed_recompute_thread(archive_home, events)
    with get_session() as s:
        b = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key == b_key)).scalar_one()
        c = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "text_complete",
            Event.dedup_key.is_(None))).scalar_one()
        b_id, c_id = b.id, c.id

    totals = rc.run(apply=True, collapse=True)
    assert totals["collapses"] == 1
    with get_session() as s:
        live = {e.id for e in s.execute(
            select(Event).where(Event.thread_id == tid)).scalars().all()}
    assert b_id in live and c_id not in live, "the keyed original survives; the NULL twin is dropped"


def test_recompute_collapse_skips_when_both_cited(archive_home) -> None:
    _pa, _pb, b_key, events = _collision_events()
    tid = _seed_recompute_thread(archive_home, events)
    with get_session() as s:
        rows = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type == "text_complete")).scalars().all()
    _cite(tid, [r.id for r in rows])
    totals = rc.run(apply=False, collapse=True)
    assert totals["collapse_skipped_both_cited"] == 1
    assert totals.get("collapses", 0) == 0


def test_recompute_collapse_no_anchor_twin(archive_home) -> None:
    """A duplicated tool-result turn has no anchor to source the id — the twin is
    still recognised from the keyed copy's own key (``_twin_of``)."""
    payload = {"tool_call_id": "tu1", "content": "output"}
    k_key = compute_dedup_key("", "tool_execution_completed", payload)
    T = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    # N (null) seeded first so it has the lower id and becomes the doomed copy.
    tid = _seed_recompute_thread(archive_home, [
        {"stream_id": "stN", "api_call_id": "cN", "event_type": "tool_execution_completed",
         "payload": dict(payload), "dedup_key": None, "occurred_at": T},
        # a same-type decoy at a different time must be skipped, not matched
        {"stream_id": "stD", "api_call_id": "cD", "event_type": "tool_execution_completed",
         "payload": {"tool_call_id": "other", "content": "z"},
         "dedup_key": compute_dedup_key("", "tool_execution_completed", {"tool_call_id": "other", "content": "z"}),
         "occurred_at": datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)},
        {"stream_id": "stK", "api_call_id": "cK", "event_type": "tool_execution_completed",
         "payload": dict(payload), "dedup_key": k_key, "occurred_at": T},
    ])
    totals = rc.run(apply=False, collapse=True)
    assert totals["collapses"] == 1


def test_recompute_no_anchor_without_collapse_leaves_null(archive_home) -> None:
    """Same shape, collapse off: an anchor-less NULL is simply left NULL (no_anchor)."""
    tid = _seed_recompute_thread(archive_home, [
        {"stream_id": "stN", "api_call_id": "cN", "event_type": "tool_execution_completed",
         "payload": {"tool_call_id": "tu1", "content": "output"}, "dedup_key": None},
    ])
    totals = rc.run(apply=False, collapse=False)
    # nothing planned; the thread makes no change
    assert totals.get("backfills", 0) == 0
    assert totals.get("collapses", 0) == 0


def test_recompute_no_anchor_twin_variants(archive_home) -> None:
    """The anchor-less ``_twin_of`` collapse across its block/cite/survivor arms:
    a block-index twin (survivor is the twin), an empty-block twin (survivor is the
    NULL row), a both-cited pair (skipped), and a NULL with no twin (left NULL)."""
    T1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 1, 1, 10, 1, 0, tzinfo=timezone.utc)
    T3 = datetime(2026, 1, 1, 10, 2, 0, tzinfo=timezone.utc)
    T4 = datetime(2026, 1, 1, 10, 3, 0, tzinfo=timezone.utc)
    bi = {"block_index": 0, "text": "hi"}
    empty = {"foo": "bar"}
    tool = {"tool_call_id": "tc", "content": "o"}
    # order matters: seed sets ascending ids in list order
    tid = _seed_recompute_thread(archive_home, [
        # block-index pair: keyed seeded first → NULL has the higher id → twin survives
        {"stream_id": "sK1", "api_call_id": "cK1", "event_type": "text_complete",
         "payload": dict(bi), "dedup_key": compute_dedup_key("", "text_complete", bi), "occurred_at": T1},
        {"stream_id": "sN1", "api_call_id": "cN1", "event_type": "text_complete",
         "payload": dict(bi), "dedup_key": None, "occurred_at": T1},
        # empty-block pair: NULL seeded first → NULL is the lower-id survivor
        {"stream_id": "sN2", "api_call_id": "cN2", "event_type": "stream_completed",
         "payload": dict(empty), "dedup_key": None, "occurred_at": T2},
        {"stream_id": "sK2", "api_call_id": "cK2", "event_type": "stream_completed",
         "payload": dict(empty), "dedup_key": compute_dedup_key("", "stream_completed", empty), "occurred_at": T2},
        # both-cited pair (tool block) → skipped, never collapsed
        {"stream_id": "sK3", "api_call_id": "cK3", "event_type": "tool_execution_completed",
         "payload": dict(tool), "dedup_key": compute_dedup_key("", "tool_execution_completed", tool), "occurred_at": T4},
        {"stream_id": "sN3", "api_call_id": "cN3", "event_type": "tool_execution_completed",
         "payload": dict(tool), "dedup_key": None, "occurred_at": T4},
        # a NULL with no keyed twin at all → left NULL (no_anchor)
        {"stream_id": "sL", "api_call_id": "cL", "event_type": "thinking_complete",
         "payload": {"block_index": 9, "thinking": "z"}, "dedup_key": None, "occurred_at": T3},
    ])
    with get_session() as s:
        both = s.execute(select(Event.id).where(
            Event.thread_id == tid, Event.event_type == "tool_execution_completed")).scalars().all()
    _cite(tid, list(both))

    totals = rc.run(apply=False, collapse=True)
    assert totals["collapses"] == 2                      # bi + empty pairs
    assert totals["collapse_skipped_both_cited"] == 1    # tool pair


def test_recompute_delete_events_clears_all_shadow_tables(archive_home) -> None:
    """``_delete_events`` sweeps events + FTS + (when present) event_search /
    event_vectors shadow rows in one statement each."""
    tid = _seed_recompute_thread(archive_home, [
        {"stream_id": "st", "api_call_id": "c", "event_type": "text_complete",
         "payload": {"block_index": 0, "text": "x"}, "dedup_key": "k1"},
    ])
    with get_session() as s:
        eid = s.execute(select(Event.id).where(Event.thread_id == tid)).scalar_one()
        # populate the two optional shadow tables the deleter guards on
        s.execute(sa_text("CREATE TABLE event_search (event_id INTEGER, body TEXT)"))
        s.execute(sa_text("CREATE TABLE event_vectors (event_id INTEGER, vec BLOB)"))
        s.execute(sa_text("INSERT INTO events_fts (event_id, thread_id, event_type, content) "
                          "VALUES (:e, :t, 'text_complete', 'x')"), {"e": eid, "t": tid})
        s.execute(sa_text("INSERT INTO event_search (event_id, body) VALUES (:e, 'x')"), {"e": eid})
        s.execute(sa_text("INSERT INTO event_vectors (event_id, vec) VALUES (:e, x'00')"), {"e": eid})
        s.commit()

    with get_session() as s:
        rc._delete_events(s, [eid])
        s.commit()

    with get_session() as s:
        assert s.execute(sa_text("SELECT count(*) FROM events WHERE id = :e"), {"e": eid}).scalar() == 0
        assert s.execute(sa_text("SELECT count(*) FROM events_fts WHERE event_id = :e"), {"e": eid}).scalar() == 0
        assert s.execute(sa_text("SELECT count(*) FROM event_search WHERE event_id = :e"), {"e": eid}).scalar() == 0
        assert s.execute(sa_text("SELECT count(*) FROM event_vectors WHERE event_id = :e"), {"e": eid}).scalar() == 0


# ══ backfill_reconcile ═══════════════════════════════════════════════════════

from thread_archive._scripts import backfill_reconcile as rec  # noqa: E402


def test_reconcile_ts_key_normalizes_all_inputs() -> None:
    assert rec._ts_key(None) == ""
    assert rec._ts_key("2026-01-01T10:00:00") == "2026-01-01T10:00:00"
    aware = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1, 10, 0, 0)
    # an aware-UTC build and a naive DB round-trip of the same instant compare equal
    assert rec._ts_key(aware) == rec._ts_key(naive)


def test_reconcile_block_and_pmid_and_norm_key() -> None:
    assert rec._block({"tool_call_id": "tu9"}) == "tool=tu9"
    assert rec._block({"block_index": 3}) == "blk=3"
    assert rec._block({}) == ""
    assert rec._pmid({"provider_data": {"provider_message_id": "pm"}}) == "pm"
    assert rec._pmid({"provider_data": "not-a-dict"}) is None
    assert rec._pmid({}) is None
    # norm_key strips only a matching {thread_id}: prefix; NULL/empty pass through
    assert rec._norm_key(None, 7) is None
    assert rec._norm_key("", 7) == ""
    assert rec._norm_key("7:abc", 7) == "abc"
    assert rec._norm_key("abc", 7) == "abc"
    assert rec._struct_anchor("text_complete", {"block_index": 0}, None) == ("text_complete", "", "blk=0")


def test_reconcile_fresh_events_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="no re-parse adapter"):
        rec._fresh_events("grok", [])


def test_reconcile_iter_pairs_yields_available_only(monkeypatch) -> None:
    cc = FakeWatcher(True, [("/tmp/a.jsonl", "sid-a")])
    monkeypatch.setattr(rec, "ClaudeCodeWatcher", lambda: cc)
    monkeypatch.setattr(rec, "cloth_watcher", lambda: FakeWatcher(False))
    pairs = list(rec._iter_pairs())
    assert pairs == [("claude-code", "/tmp/a.jsonl", "sid-a")]


def test_reconcile_plan_counts_unmatched_fresh_as_dropped(archive_home) -> None:
    """A fresh event that matches no persisted row is candidate dropped content."""
    from sqlalchemy import delete
    tid, _ = _import_cc(archive_home)
    with get_session() as s:
        # drop the ide_context row so its fresh rebuild matches nothing persisted
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type == "ide_context"))
        s.commit()
    with get_session() as s:
        plan = rec.plan_thread(s, tid, "claude-code", LINES)
    assert plan.stats.get("unmatched_fresh", 0) >= 1


def test_reconcile_run_skips_unmapped_source(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "no-such-source")]))
    totals = rec.run(apply=False)
    assert totals.get("threads", 0) == 0


def test_reconcile_run_counts_plan_errors(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))

    def _boom(path):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(rec, "read_session_lines", _boom)
    totals = rec.run(apply=False)
    assert totals["plan_errors"] == 1
    assert totals["threads"] == 1


def test_reconcile_run_respects_limit(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec, "_iter_pairs", lambda: iter([
        ("claude-code", f, "proj:s1"),
        ("claude-code", f, "proj:s1"),
    ]))
    totals = rec.run(apply=False, limit=1)
    assert totals["threads"] == 1


def test_reconcile_run_flags_unsafe_on_key_drift(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().first()
        s.execute(update(Event).where(Event.id == ev.id).values(dedup_key="drifted-key"))
        s.commit()
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    totals = rec.run(apply=False)
    assert totals["unsafe_threads"] == 1
    assert totals["warnings"] >= 1


def test_reconcile_run_apply_without_backup(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    _null_all_keys(tid)
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    totals = rec.run(apply=True)  # no backup_path → the backup-write branch is skipped
    assert totals["backfills"] > 0
    assert any(k for k in _keys(tid).values())


def test_reconcile_run_apply_with_backup(archive_home, tmp_path, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    _null_all_keys(tid)
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    backup = tmp_path / "reconcile-backup.jsonl"
    totals = rec.run(apply=True, backup_path=backup)
    assert totals["backfills"] > 0
    rows = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert rows and rows[0]["thread_id"] == tid
    keyed = {eid for eid, k in _keys(tid).items() if k}
    assert set(rows[0]["backfill_ids"]) == keyed


def test_reconcile_main_dry_and_apply(archive_home, monkeypatch, capsys) -> None:
    tid, f = _import_cc(archive_home)
    _null_all_keys(tid)
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    rec.main([])
    assert "[DRY-RUN]" in capsys.readouterr().out
    monkeypatch.setattr(rec, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    rec.main(["--apply", "-v"])
    assert "[APPLIED]" in capsys.readouterr().out
    assert any(k for k in _keys(tid).values())


# ══ recover_dropped_events ═══════════════════════════════════════════════════

from thread_archive._scripts import recover_dropped_events as rec2  # noqa: E402


def _strip_recoverable(tid: int) -> int:
    from sqlalchemy import delete
    with get_session() as s:
        before = len(s.execute(select(Event).where(Event.thread_id == tid)).scalars().all())
        s.execute(delete(Event).where(
            Event.thread_id == tid, Event.event_type.in_(list(rec2.RECOVERABLE_TYPES))))
        s.commit()
    return before


def test_recover_existing_ignores_null_key_rows(archive_home) -> None:
    tid, _ = _import_cc(archive_home)
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.dedup_key.is_not(None))).scalars().first()
        s.execute(update(Event).where(Event.id == ev.id).values(dedup_key=None))
        s.commit()
    with get_session() as s:
        keys, anchor = rec2._existing(s, tid)
    # the nulled row contributes neither a key nor an anchor entry
    assert None not in keys
    assert all(v is not None for v in keys)


def test_recover_iter_pairs_yields_available_only(monkeypatch) -> None:
    cc = FakeWatcher(True, [("/tmp/x.jsonl", "sid-x")])
    monkeypatch.setattr(rec2, "ClaudeCodeWatcher", lambda: cc)
    monkeypatch.setattr(rec2, "cloth_watcher", lambda: FakeWatcher(False))
    assert list(rec2._iter_pairs()) == [("claude-code", "/tmp/x.jsonl", "sid-x")]


def test_recover_run_skips_unmapped_source(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "no-such-source")]))
    totals = rec2.run(apply=False)
    assert totals["threads_mapped"] == 0
    assert totals["files_seen"] == 1


def test_recover_run_respects_limit(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    _strip_recoverable(tid)
    monkeypatch.setattr(rec2, "_iter_pairs", lambda: iter([
        ("claude-code", f, "proj:s1"),
        ("claude-code", f, "proj:s1"),
    ]))
    totals = rec2.run(apply=False, limit=1)
    assert totals["files_seen"] == 2
    assert totals["threads_mapped"] == 1


def test_recover_run_counts_read_errors(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))

    def _boom(path):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(rec2, "read_session_lines", _boom)
    totals = rec2.run(apply=False)
    assert totals["read_errors"] == 1


def test_recover_run_counts_write_errors(archive_home, monkeypatch) -> None:
    tid, f = _import_cc(archive_home)
    _strip_recoverable(tid)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))

    def _boom(session, rows):
        raise RuntimeError("write failed")

    monkeypatch.setattr(rec2, "write_events", _boom)
    totals = rec2.run(apply=True)
    assert totals["write_errors"] == 1


def test_recover_run_nothing_to_recover_is_noop(archive_home, monkeypatch) -> None:
    """A whole thread (nothing was dropped) plans no rows — the per-thread skip."""
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    totals = rec2.run(apply=False)
    assert totals["threads_mapped"] == 1
    assert totals["threads_recovered"] == 0
    assert totals["events_recovered"] == 0


def test_recover_main_prints_error_counts(archive_home, monkeypatch, capsys) -> None:
    tid, f = _import_cc(archive_home)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))

    def _boom(path):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(rec2, "read_session_lines", _boom)
    rec2.main([])
    out = capsys.readouterr().out
    assert "read errors" in out


def test_recover_main_dry_and_apply(archive_home, monkeypatch, capsys) -> None:
    tid, f = _import_cc(archive_home)
    n_full = len(_keys(tid))
    _strip_recoverable(tid)
    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    rec2.main([])
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "events recovered" in out

    monkeypatch.setattr(rec2, "_iter_pairs",
                        lambda: iter([("claude-code", f, "proj:s1")]))
    rec2.main(["--apply", "-v"])
    assert "[APPLIED]" in capsys.readouterr().out
    assert len(_keys(tid)) == n_full


# ══ backfill_codex_model ═════════════════════════════════════════════════════

from thread_archive._scripts import backfill_codex_model as cx  # noqa: E402

CODEX_SESSION = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "sess", "cwd": "/proj", "model_provider": "openai"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "first turn", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "task_started", "turn_id": "t1"}},
    {"type": "turn_context", "timestamp": "2026-01-01T10:00:03Z",
     "payload": {"turn_id": "t1", "model": "gpt-5.6-sol"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:04Z",
     "payload": {"type": "agent_message", "message": "answered by sol"}},
]

# A codex old enough to name no model anywhere — nothing to recover from.
CODEX_MODELLESS = [
    {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"id": "old", "cwd": "/proj"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:01Z",
     "payload": {"type": "user_message", "message": "hello", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:02Z",
     "payload": {"type": "agent_message", "message": "hi"}},
]


def _seed_codex(archive_home, lines=CODEX_SESSION, name="codex.jsonl", source_id="s1") -> int:
    init_db()
    f = archive_home / name
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(codex_mod, "_codex_line_model", lambda line: None)
        return import_codex_session_incremental(f, source_id).thread_id


def _codex_models(tid: int) -> set[str]:
    with get_session() as s:
        rows = s.execute(select(Event.payload).where(
            Event.thread_id == tid, Event.event_type.in_(cx.MODEL_EVENTS))).all()
    return {p["model"] for (p,) in rows}


def test_codex_block_model_guards_non_dicts() -> None:
    assert cx._block_model({"data": None}) is None
    assert cx._block_model({"data": {"raw": None}}) is None
    assert cx._block_model({"data": {"codex_line_type": "turn_context",
                                     "raw": {"turn_id": "t", "model": "gpt-x"}}}) == "gpt-x"


def test_codex_as_utc_variants() -> None:
    assert cx._as_utc("not-a-timestamp") is None
    assert cx._as_utc(12345) is None
    aware = cx._as_utc("2026-01-01T10:00:00Z")
    assert aware is not None and aware.tzinfo is not None
    naive = cx._as_utc(datetime(2026, 1, 1, 10, 0, 0))
    assert naive.tzinfo is not None


def test_codex_model_at_edge_cases() -> None:
    assert cx._model_at([], datetime(2026, 1, 1, tzinfo=timezone.utc)) is None
    tl = [(datetime(2026, 1, 1, 10, tzinfo=timezone.utc), "m1")]
    assert cx._model_at(tl, None) is None
    # a turn ending before the first change point still gets the first model named
    assert cx._model_at(tl, datetime(2026, 1, 1, 9, tzinfo=timezone.utc)) == "m1"
    assert cx._model_at(tl, datetime(2026, 1, 1, 11, tzinfo=timezone.utc)) == "m1"


def test_codex_patch_truth_missing_file_returns_zero(archive_home) -> None:
    assert cx.patch_truth(999999, {}) == 0


def test_codex_patch_truth_passes_blank_and_torn_lines_through(archive_home) -> None:
    tid = _seed_codex(archive_home)
    from thread_archive._truth.jsonl_log import _shard_depth, _thread_file, log_dir
    path = _thread_file(log_dir(), tid, _shard_depth(log_dir()))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")                      # blank line — copied through untouched
        fh.write('{"type": "event", "id": \n')  # torn/garbage line — passed through
    cx.run(apply=True)
    lines = path.read_text(encoding="utf-8").split("\n")
    assert "" in lines
    assert '{"type": "event", "id": ' in lines


def test_codex_rekey_rehashes_and_leaves_tailless_keys(archive_home) -> None:
    payload = {"model": "gpt-5.6-sol", "provider_data": {"provider_message_id": "x"}}
    # a codex (timestamp) anchor contains colons → split from the right
    key = cx.rekey("2026-01-01T10:00:02Z:api_request_started::0123456789abcdef", payload)
    anchor, etype, block, tail = key.rsplit(":", 3)
    assert (anchor, etype, block) == ("2026-01-01T10:00:02Z", "api_request_started", "")
    assert len(tail) == 16
    # an anchorless (c=) key: the anchor moves with the content hash
    anchorless = cx.rekey("c=0123456789abcdef:api_request_completed::0123456789abcdef", payload)
    assert anchorless.startswith("c=") and anchorless.startswith(f"c={anchorless.rsplit(':', 1)[-1]}:")
    # a key with no hash tail carries no content claim → returned unchanged
    assert cx.rekey("some-legacy-key", payload) == "some-legacy-key"


def test_codex_rollout_timeline_reads_change_points(archive_home) -> None:
    roll = archive_home / "rollout.jsonl"
    roll.write_text("\n".join(json.dumps(ln) for ln in CODEX_SESSION) + "\n", encoding="utf-8")
    tl = cx.rollout_timeline(roll)
    assert tl and tl[-1][1] == "gpt-5.6-sol"
    assert all(hasattr(when, "tzinfo") for when, _ in tl)


def test_codex_apply_with_backup(archive_home, tmp_path) -> None:
    tid = _seed_codex(archive_home)
    backup = tmp_path / "codex-backup.jsonl"
    totals = cx.run(apply=True, backup_path=backup)
    assert totals.get("write_errors", 0) == 0
    rows = [json.loads(ln) for ln in backup.read_text().splitlines()]
    assert rows and all(r["old_model"] == "codex" for r in rows)


def test_codex_rerun_after_apply_finds_no_placeholders(archive_home) -> None:
    """Once repaired, every MODEL_EVENT carries a real name — the thread holds no
    placeholder, so a re-run plans nothing (the 'nothing to do' skip)."""
    tid = _seed_codex(archive_home)
    cx.run(apply=True)
    again = cx.run(apply=True)
    assert again.get("threads_with_placeholder", 0) == 0
    assert again.get("events", 0) == 0


def test_codex_modelless_thread_stays_placeholder(archive_home) -> None:
    """Placeholders with no model to resolve them to are left untouched, not guessed."""
    tid = _seed_codex(archive_home, lines=CODEX_MODELLESS, name="old.jsonl", source_id="old")
    totals = cx.run(apply=True)
    assert totals["threads_with_placeholder"] == 1
    assert totals["threads_unresolved"] == 1
    assert totals.get("events", 0) == 0
    assert _codex_models(tid) == {"codex"}


def test_codex_codex_threads_limit(archive_home) -> None:
    _seed_codex(archive_home, source_id="s1", name="a.jsonl")
    _seed_codex(archive_home, source_id="s2", name="b.jsonl")
    with get_session() as s:
        assert len(cx._codex_threads(s, None)) == 2
        assert len(cx._codex_threads(s, 1)) == 1


def test_codex_rollout_index_from_available_watcher(archive_home, monkeypatch) -> None:
    fake = FakeWatcher(True, [("/tmp/roll.jsonl", "s1")])
    monkeypatch.setattr(cx, "codex_watcher", lambda: fake)
    assert cx.rollout_index() == {"s1": "/tmp/roll.jsonl"}
    monkeypatch.setattr(cx, "codex_watcher", lambda: FakeWatcher(False))
    assert cx.rollout_index() == {}


def test_codex_run_counts_rollout_errors(archive_home, monkeypatch) -> None:
    tid = _seed_codex(archive_home, source_id="s1")
    bad = archive_home / "rollout.jsonl"
    bad.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cx, "rollout_index", lambda: {"s1": bad})

    def _boom(path):
        raise RuntimeError("torn rollout")

    monkeypatch.setattr(cx, "rollout_timeline", _boom)
    totals = cx.run(apply=False)
    assert totals["rollout_errors"] == 1


def test_codex_apply_handles_event_without_dedup_key(archive_home) -> None:
    tid = _seed_codex(archive_home)
    # blank one placeholder event's key so the rekey branch is skipped for it
    with get_session() as s:
        ev = s.execute(select(Event).where(
            Event.thread_id == tid, Event.event_type.in_(cx.MODEL_EVENTS))).scalars().first()
        s.execute(update(Event).where(Event.id == ev.id).values(dedup_key=None))
        s.commit()
        target = ev.id
    totals = cx.run(apply=True)
    assert totals.get("write_errors", 0) == 0
    with get_session() as s:
        row = s.get(Event, target)
    assert row.payload["model"] == "gpt-5.6-sol"
    assert row.dedup_key is None  # stayed None; no key to rehash


def test_codex_run_counts_write_errors(archive_home, monkeypatch) -> None:
    tid = _seed_codex(archive_home)
    # force every rewritten event onto one dedup_key → the commit trips the unique index
    monkeypatch.setattr(cx, "rekey", lambda key, payload: "collide")
    totals = cx.run(apply=True)
    assert totals["write_errors"] >= 1
    # the failed thread rolled back — the placeholder is still in place, not half-written
    assert _codex_models(tid) == {"codex"}


def test_codex_main_dry_and_apply(archive_home, capsys) -> None:
    tid = _seed_codex(archive_home)
    cx.main([])
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "codex threads seen" in out

    cx.main(["--apply", "-v"])
    assert "[APPLIED]" in capsys.readouterr().out
    assert _codex_models(tid) == {"gpt-5.6-sol"}


def test_codex_main_reports_conflicts_and_rollout_errors(archive_home, monkeypatch, capsys) -> None:
    """A rollout that both disagrees (conflict) and, for a second thread, is
    unreadable (rollout error) surfaces both footer lines in ``main``."""
    tid = _seed_codex(archive_home, source_id="s1", name="a.jsonl")
    # rollout names a different model for s1's turn → conflict; s-missing raises
    lying = [dict(ln, payload=dict(ln["payload"], model="gpt-9-imaginary"))
             if ln["type"] == "turn_context" else ln for ln in CODEX_SESSION]
    roll = archive_home / "rollout-s1.jsonl"
    roll.write_text("\n".join(json.dumps(ln) for ln in lying) + "\n", encoding="utf-8")

    real_timeline = cx.rollout_timeline

    def _timeline(path):
        if path == roll:
            return real_timeline(path)
        raise RuntimeError("torn rollout")

    # s1 → conflicting rollout; a phantom source id → unreadable rollout
    _seed_codex(archive_home, source_id="s2", name="b.jsonl")
    bad = archive_home / "rollout-s2.jsonl"
    bad.write_text("garbage\n", encoding="utf-8")
    monkeypatch.setattr(cx, "rollout_index", lambda: {"s1": roll, "s2": bad})
    monkeypatch.setattr(cx, "rollout_timeline", _timeline)

    cx.main(["--apply"])
    out = capsys.readouterr().out
    assert "[APPLIED]" in out
    assert "archive/rollout disagree" in out
    assert "rollout errors" in out
