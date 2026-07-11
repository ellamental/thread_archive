"""Integrity hardening: the guarantees behind the drain/reindex/verify seams.

- a COMMIT that fails *after* its truth drain is compensated: the drained batch
  is truncated back out of the truth (no resurrection on reindex)
- reindex refuses to publish a build that lost committed threads, or one that is
  relationally inconsistent (foreign_key_check), with --salvage as the override
- a *new* hash mismatch fails ``verify(hashes=True)``'s ``ok`` (once — the
  stamped baseline absorbs it)
- verify reports declared-schema gaps (dropped index / column) and reindex heals
- backup checkpoints before it verifies, so verify_ok describes the mirrored tree
- health.json survives concurrent writers (locked read-modify-write)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text

import thread_archive as ta
from thread_archive._store import Event, get_engine, get_session
from thread_archive._truth import jsonl_log

from .helpers import event_count, import_cc_session, one_thread_file


def _now() -> datetime:
    return datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


# ── failed COMMIT after the drain ─────────────────────────────────────────────
def test_failed_commit_after_drain_rolls_the_truth_back(archive_home, tmp_path):
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    truth_before = tf.read_bytes()
    events_before = event_count()
    with get_session() as s:
        tid = s.execute(text("SELECT id FROM threads")).scalar()

    engine = get_engine()

    def _boom(conn):
        raise RuntimeError("simulated commit failure")

    sa_event.listen(engine, "commit", _boom)
    try:
        with pytest.raises(Exception, match="simulated commit failure"):
            with get_session() as s:
                jsonl_log.write_events(s, [Event(
                    thread_id=tid, stream_id="x", event_type="user_message_sent",
                    payload={"text": "doomed"}, occurred_at=_now(),
                    dedup_key="doomed-1",
                )])
                s.commit()
    finally:
        sa_event.remove(engine, "commit", _boom)

    # Drop pooled connections: the failed transaction's connection lingers in
    # the pool with its (never-committed) transaction open, and a same-pool
    # read would see its phantom rows.
    engine.dispose()

    # SQLite rejected the transaction, so the truth must not keep it either.
    assert event_count() == events_before
    assert tf.read_bytes() == truth_before
    assert ta.verify()["ok"] is True
    # And a rebuild does not resurrect the rejected batch.
    counts = ta.reindex()
    assert counts["events"] == events_before


def test_compensation_leaves_files_another_writer_touched(archive_home, tmp_path):
    """The undo is size-guarded: if the file changed after the drain (another
    writer appended), compensation must leave it alone — degrade to the
    documented JSONL ⊇ SQLite direction rather than chop foreign records."""
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    with get_session() as s:
        tid = s.execute(text("SELECT id FROM threads")).scalar()

    engine = get_engine()
    foreign_line = json.dumps({"type": "event", "id": 999999, "thread_id": tid,
                               "stream_id": "x", "event_type": "user_message_sent",
                               "payload": {"text": "foreign"}}) + "\n"

    def _boom_and_interleave(conn):
        # Simulate another process appending between drain and (failed) commit.
        jsonl_log.reset_handles()
        with open(tf, "a", encoding="utf-8") as fh:
            fh.write(foreign_line)
        raise RuntimeError("simulated commit failure")

    sa_event.listen(engine, "commit", _boom_and_interleave)
    try:
        with pytest.raises(Exception, match="simulated commit failure"):
            with get_session() as s:
                jsonl_log.write_events(s, [Event(
                    thread_id=tid, stream_id="x", event_type="user_message_sent",
                    payload={"text": "doomed"}, occurred_at=_now(),
                    dedup_key="doomed-2",
                )])
                s.commit()
    finally:
        sa_event.remove(engine, "commit", _boom_and_interleave)

    content = tf.read_text(encoding="utf-8")
    assert "foreign" in content, "the interleaved writer's record must survive"
    assert "doomed" in content, "size changed — the undo must not have run"


# ── reindex structural gates ──────────────────────────────────────────────────
def _clobber_thread_name(archive_home, victim_name: str, twin_name: str) -> None:
    """Rewrite ``twin_name``'s truth thread record to carry ``victim_name`` —
    the name-conflict that makes the FK-OFF OR REPLACE load drop the victim
    thread row while its events survive."""
    files = {f.stem: f for f in (archive_home / "truth" / "threads").rglob("*.jsonl")}
    with get_session() as s:
        victim_id, twin_id = (
            s.execute(text("SELECT id FROM threads WHERE name = :n"), {"n": n}).scalar()
            for n in (victim_name, twin_name)
        )
    twin_file = files[str(twin_id)]
    lines = twin_file.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        rec = json.loads(ln)
        if rec.get("type") == "thread":
            rec["name"] = victim_name
            lines[i] = json.dumps(rec)
    twin_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()


def _thread_names() -> set[str]:
    with get_session() as s:
        return {r[0] for r in s.execute(text("SELECT name FROM threads"))}


def test_reindex_refuses_committed_thread_loss(archive_home, tmp_path):
    ra = import_cc_session(tmp_path, "sessA")
    rb = import_cc_session(tmp_path, "sessB")
    names = sorted(_thread_names())
    _clobber_thread_name(archive_home, names[0], names[1])

    with pytest.raises(RuntimeError, match=r"lose .* thread\(s\)"):
        ta.reindex()
    # The old index is untouched.
    assert _thread_names() == set(names)
    # --salvage is the deliberate override for the lossy publish.
    counts = ta.reindex(salvage=True)
    assert counts["threads"] == 1
    del ra, rb


def test_reindex_refuses_fk_broken_build_without_baseline(archive_home, tmp_path):
    import_cc_session(tmp_path, "sessA")
    import_cc_session(tmp_path, "sessB")
    names = sorted(_thread_names())
    _clobber_thread_name(archive_home, names[0], names[1])
    # No previous index → no regression baseline; the FK gate must still refuse
    # to publish a build whose OR REPLACE load orphaned a thread's events.
    ta.close()
    for suffix in ("", "-wal", "-shm"):
        (archive_home / f"index.db{suffix}").unlink(missing_ok=True)

    with pytest.raises(RuntimeError, match="foreign_key_check"):
        ta.reindex()
    assert not (archive_home / "index.db.rebuild").exists()


# ── new hash mismatches fail verify ───────────────────────────────────────────
def test_new_hash_mismatch_fails_verify_once(archive_home, tmp_path):
    import_cc_session(tmp_path)
    assert ta.verify(hashes=True)["ok"] is True  # clean baseline stamped

    tf = one_thread_file(archive_home)
    lines = tf.read_text(encoding="utf-8").splitlines()
    events = [(i, json.loads(ln)) for i, ln in enumerate(lines)
              if '"type": "event"' in ln]
    idx, rec = next((i, r) for i, r in reversed(events) if r.get("dedup_key"))
    rec["payload"] = {"content": "rotted"}
    lines[idx] = json.dumps(rec)
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    res = ta.verify(hashes=True)
    assert res["hashes"]["truth"]["mismatched"] == 1
    assert res["hashes"]["new_mismatches"] is True
    assert res["ok"] is False, "detected corruption must fail health"

    # The stamped baseline absorbs the count: seen once, then delta-green.
    res2 = ta.verify(hashes=True)
    assert res2["hashes"]["new_mismatches"] is False
    assert res2["ok"] is True


# ── declared-schema parity ────────────────────────────────────────────────────
def test_verify_reports_underenforced_schema_and_reindex_heals(archive_home, tmp_path):
    import_cc_session(tmp_path)
    assert ta.verify()["schema"]["ok"] is True

    with get_session() as s:
        conn = s.connection().connection
        conn.execute("DROP INDEX uq_events_thread_dedup")
        conn.execute("ALTER TABLE threads DROP COLUMN search_description")
        s.commit()

    res = ta.verify()
    assert res["ok"] is False
    assert "uq_events_thread_dedup" in res["schema"]["missing_indexes"]
    assert "threads.search_description" in res["schema"]["missing_columns"]

    ta.reindex()  # a fresh build carries the full declared schema
    assert ta.verify()["schema"]["ok"] is True


def test_verify_reports_missing_unique_constraint(archive_home, tmp_path):
    import_cc_session(tmp_path)
    with get_session() as s:
        conn = s.connection().connection
        # Rebuild import_state without its UNIQUE(source, source_id).
        conn.execute("ALTER TABLE import_state RENAME TO import_state_old")
        conn.execute(
            "CREATE TABLE import_state AS SELECT * FROM import_state_old"
        )
        conn.execute("DROP TABLE import_state_old")
        s.commit()

    res = ta.verify()
    assert res["ok"] is False
    assert any(
        m.startswith("import_state(") for m in res["schema"]["missing_unique_constraints"]
    )


# ── backup order: checkpoint before verify ────────────────────────────────────
def test_backup_checkpoints_before_verifying(archive_home, tmp_path, monkeypatch):
    import_cc_session(tmp_path)
    calls: list[str] = []

    import thread_archive.api as api
    from thread_archive import _truth as truth

    real_checkpoint, real_verify = truth.checkpoint, api.verify
    monkeypatch.setattr(
        truth, "checkpoint",
        lambda *a, **k: (calls.append("checkpoint"), real_checkpoint(*a, **k))[1],
    )
    monkeypatch.setattr(
        api, "verify",
        lambda *a, **k: (calls.append("verify"), real_verify(*a, **k))[1],
    )
    ta.backup(str(tmp_path / "mirror"))
    assert calls.index("checkpoint") < calls.index("verify"), calls


# ── health.json concurrency ───────────────────────────────────────────────────
def test_record_health_survives_concurrent_writers(archive_home, tmp_path):
    ta.open_archive()
    from thread_archive.api import _read_health, _record_health

    keys = [f"writer_{i}" for i in range(8)]

    def hammer(key: str) -> None:
        for n in range(25):
            _record_health(key, {"ok": True, "n": n})

    threads = [threading.Thread(target=hammer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    health = _read_health()
    assert set(keys) <= set(health), "no writer's record may be lost"
    assert all(health[k]["n"] == 24 for k in keys)


# ── backup mirror is drain-consistent ─────────────────────────────────────────
def test_backup_mirror_holds_the_truth_write_lock(archive_home, tmp_path, monkeypatch):
    """The mirror traversal runs under the truth-write mutex, so no append batch
    can land in (or be rolled back out of) a truth file mid-copy."""
    import fcntl
    import os

    import_cc_session(tmp_path)
    import thread_archive.api as api

    real = api._mirror_dir
    seen: dict[str, bool] = {}

    def probe(*a, **k):
        fd = os.open(jsonl_log._truth_write_lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                seen["held"] = False
            except OSError:
                seen["held"] = True
        finally:
            os.close(fd)
        return real(*a, **k)

    monkeypatch.setattr(api, "_mirror_dir", probe)
    ta.backup(str(tmp_path / "mirror"))
    assert seen["held"] is True, "the mirror must run inside the truth-write lock"


# ── new backup hash mismatches fail verify ────────────────────────────────────
def test_new_backup_hash_mismatch_fails_verify_once(archive_home, tmp_path):
    """Rot at rest in the mirror (never re-copied — size+mtime skip) must fail
    ``verify --hashes --backup``, once, with the same baseline absorption as the
    live scan."""
    import_cc_session(tmp_path)
    mirror = tmp_path / "mirror"
    ta.backup(str(mirror))
    res = ta.verify(hashes=True, backup=str(mirror))
    assert res["ok"] is True  # clean baselines stamped (live + mirror)

    tf = next((mirror / "threads").rglob("*.jsonl"))
    lines = tf.read_text(encoding="utf-8").splitlines()
    events = [(i, json.loads(ln)) for i, ln in enumerate(lines)
              if '"type": "event"' in ln]
    idx, rec = next((i, r) for i, r in reversed(events) if r.get("dedup_key"))
    rec["payload"] = {"content": "rotted at rest"}
    lines[idx] = json.dumps(rec)
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = ta.verify(hashes=True, backup=str(mirror))
    assert res["backup"]["hashes"]["mismatched"] == 1
    assert res["backup"]["hashes"]["new_mismatches"] is True
    assert "backup_hashes" in res["failed_components"]
    assert res["ok"] is False, "mirror rot must fail health"
    assert res["hashes"]["truth"]["mismatched"] == 0  # the live truth is clean

    # The recorded baseline absorbs the count: seen once, then delta-green.
    res2 = ta.verify(hashes=True, backup=str(mirror))
    assert res2["backup"]["hashes"]["new_mismatches"] is False
    assert res2["ok"] is True
