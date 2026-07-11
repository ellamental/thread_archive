"""Tests for the eighth-pass integrity hardening — the repair path + operational health.

1. ``scan_truth_counts`` classifies parse errors (torn tail vs interior).
2. ``archive repair`` quarantines unparseable lines (bytes preserved in the
   ledger) and restores the committed events they shadowed from the index —
   a red ``verify`` goes green again.
3. Repair is idempotent (a clean archive repairs to zero actions), previews
   with ``dry_run``, and heals index ⊃ truth drift up to a fully deleted
   thread file.
4. Shallow ``verify`` fails on FTS shadow↔FTS5 drift; deep verify splits the
   coverage gap into genuinely-unindexed (fails) vs empty-extract (reported).
5. ``verify`` / ``backup`` record their outcomes in the manifest and
   ``status`` surfaces them.
6. The watcher's import-state stamp moves on watermark-only changes, so the
   maintenance snapshot can't go stale when no events were created.
"""

from __future__ import annotations

import json

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log, scan_truth_counts
from thread_archive.truth.repair import FRAGMENTS_FILE, QUARANTINE_SUBDIR

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity eight"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back crazy eights"}]}}


def _import_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    user = {**USER, "uuid": f"u-{name}",
            "message": {"role": "user", "content": f"hello integrity eight {name}"}}
    asst = {**ASSISTANT, "uuid": f"a-{name}"}
    f.write_text("\n".join(json.dumps(ln) for ln in (user, asst)) + "\n", encoding="utf-8")
    ta.import_path(f)


def _one_thread_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


def _event_count() -> int:
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM events")).scalar()


def _corrupt_event_line(tf, marker='{"type": "event", "id": corrupted beyond'):
    """Replace the file's first event line with an unparseable one; returns the
    original line so the test can assert on what was lost."""
    lines = tf.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if '"type": "event"' in ln)
    original = lines[idx]
    lines[idx] = marker
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()
    return original


# ── 1. parse-error classification ─────────────────────────────────────────────
def test_scan_classifies_torn_tail_vs_interior(archive_home, tmp_path):
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    _corrupt_event_line(tf)  # interior damage
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99, "torn mid-wri')  # torn tail, no newline
    jsonl_log.reset_handles()

    counts = scan_truth_counts()
    assert counts["parse_errors"] == 2
    assert counts["parse_errors_interior"] == 1
    assert counts["parse_errors_torn_tail"] == 1


# ── 2. repair: quarantine + restore-from-index ────────────────────────────────
def test_repair_quarantines_and_restores_committed_event(archive_home, tmp_path):
    _import_session(tmp_path)
    before = _event_count()
    tf = _one_thread_file(archive_home)
    original = _corrupt_event_line(tf)
    damaged_id = json.loads(original)["id"]

    assert ta.verify()["ok"] is False  # damage is visible…

    res = ta.repair()
    assert res["fragments_quarantined"] == 1
    assert res["events_restored_from_index"] == 1

    # …and repair makes verify green again without touching the index.
    assert _event_count() == before
    v = ta.verify()
    assert v["ok"] is True, v

    # The fragment's bytes survive in the ledger.
    ledger = archive_home / "truth" / QUARANTINE_SUBDIR / FRAGMENTS_FILE
    recs = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1
    assert "corrupted beyond" in recs[0]["raw"]
    assert recs[0]["file"] == str(tf.relative_to(archive_home / "truth"))

    # The restored line matches the index row it was re-emitted from.
    restored = [
        json.loads(ln) for ln in tf.read_text(encoding="utf-8").splitlines()
        if json.loads(ln).get("id") == damaged_id and json.loads(ln).get("type") == "event"
    ]
    assert len(restored) == 1
    assert restored[0]["payload"] == json.loads(original)["payload"]

    # A reindex of the repaired truth publishes cleanly (no committed loss).
    counts = ta.reindex()
    assert counts["parse_errors_interior"] == 0
    assert _event_count() == before


def test_repair_dry_run_touches_nothing_and_is_idempotent(archive_home, tmp_path):
    _import_session(tmp_path)
    tf = _one_thread_file(archive_home)
    _corrupt_event_line(tf)
    damaged_text = tf.read_text(encoding="utf-8")

    res = ta.repair(dry_run=True)
    assert res["fragments_quarantined"] == 1
    assert res["events_restored_from_index"] == 1
    assert tf.read_text(encoding="utf-8") == damaged_text  # untouched
    assert not (archive_home / "truth" / QUARANTINE_SUBDIR).exists()

    ta.repair()
    # A second pass finds a clean archive: zero actions.
    res = ta.repair()
    assert res["fragments_quarantined"] == 0
    assert res["events_restored_from_index"] == 0
    assert res["files_damaged"] == 0


def test_repair_recreates_deleted_thread_file(archive_home, tmp_path):
    _import_session(tmp_path)
    before = _event_count()
    tf = _one_thread_file(archive_home)
    tf.unlink()  # index ⊃ truth: the forbidden direction
    jsonl_log.reset_handles()

    res = ta.repair()
    assert res["events_restored_from_index"] == before
    assert res["thread_records_restored"] == 1
    assert tf.exists()
    recs = [json.loads(ln) for ln in tf.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["type"] == "thread"
    assert ta.verify()["ok"] is True


# ── 4. FTS parity ─────────────────────────────────────────────────────────────
def test_shallow_verify_fails_on_fts5_drift(archive_home, tmp_path):
    _import_session(tmp_path)
    assert ta.verify()["ok"] is True
    with get_session() as s:
        s.execute(text("DELETE FROM event_search WHERE rowid IN "
                       "(SELECT rowid FROM event_search LIMIT 1)"))
        s.commit()
    v = ta.verify()
    assert v["ok"] is False
    assert v["fts"]["fts5_rows"] == v["fts"]["shadow_rows"] - 1


def test_deep_verify_splits_unindexed_from_empty_extract(archive_home, tmp_path):
    _import_session(tmp_path)
    with get_session() as s:
        # Strip one indexed event from BOTH surfaces: counts stay in parity, so
        # only the coverage re-extract can see it.
        eid = s.execute(text(
            "SELECT event_id FROM events_fts WHERE event_type = 'user_message_sent' LIMIT 1"
        )).scalar()
        s.execute(text("DELETE FROM events_fts WHERE event_id = :e"), {"e": eid})
        s.execute(text("DELETE FROM event_search WHERE event_id = :e"), {"e": eid})
        s.commit()
    v = ta.verify()
    assert v["ok"] is True  # shallow parity can't see it…
    v = ta.verify(deep=True)
    assert v["ok"] is False  # …the deep re-extract can
    assert v["deep"]["fts"]["unindexed_events"] == 1
    assert v["deep"]["fts"]["unindexed_sample"] == [eid]


# ── 5. operational health records ─────────────────────────────────────────────
def test_verify_and_backup_record_outcomes_in_status(archive_home, tmp_path):
    _import_session(tmp_path)
    st = ta.status()
    assert st["last_verify"] is None and st["last_backup"] is None

    assert ta.verify()["ok"] is True
    st = ta.status()
    assert st["last_verify"]["ok"] is True
    assert st["last_verify"]["at"]

    dest = tmp_path / "mirror"
    res = ta.backup(str(dest))
    assert res["mirror_complete"] is True
    st = ta.status()
    assert st["last_backup"]["ok"] is True
    assert st["last_backup"]["dest"] == str(dest)

    # A failing verify is recorded as such.
    tf = _one_thread_file(archive_home)
    _corrupt_event_line(tf)
    assert ta.verify()["ok"] is False
    assert ta.status()["last_verify"]["ok"] is False


# ── 6. watcher maintenance stamp ──────────────────────────────────────────────
def test_import_state_stamp_moves_on_watermark_only_change(archive_home, tmp_path):
    from thread_archive.importers._state import upsert_import_state
    from thread_archive.watcher.daemon import Watcher

    _import_session(tmp_path)
    stamp1 = Watcher._import_state_stamp()
    assert stamp1 is not None

    # A watermark-only change: no events, no thread — just a cursor advance,
    # exactly what adopt_if_unwatermarked / an empty-content poll performs.
    with get_session() as s:
        upsert_import_state(
            s, source="codex", source_id="wm-only", thread_id=None,
            last_line_count=10, last_file_size=1000, last_message_uuid=None,
        )
        s.commit()
    stamp2 = Watcher._import_state_stamp()
    assert stamp2 != stamp1
