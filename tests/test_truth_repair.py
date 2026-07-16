"""Truth-file damage: crash-torn tails, all-or-nothing drains, and ``archive repair``.

* A torn truth tail (crash mid-append) is newline-repaired before the next
  append, so the fragment can't consume a later valid event.
* The before-commit truth drain is all-or-nothing: a failure partway rolls the
  touched files back to their pre-drain size, so a failed batch leaves no
  partial records for reindex to resurrect.
* ``scan_truth_counts`` classifies parse errors (torn tail vs interior).
* ``archive repair`` quarantines unparseable lines (bytes preserved in the
  ledger) and restores the committed events they shadowed from the index — a
  red ``verify`` goes green again. Repair is idempotent, previews with
  ``dry_run``, and heals index ⊃ truth drift up to a fully deleted thread file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from thread_archive import _api as ta
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._truth import jsonl_log, scan_truth_counts
from thread_archive._truth.repair import FRAGMENTS_FILE, QUARANTINE_SUBDIR

from .helpers import corrupt_event_line, event_count, import_cc_session, one_thread_file


def _now():
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


# ── torn-tail isolation on the write path ─────────────────────────────────────
def test_torn_tail_is_repaired_and_cannot_consume_the_next_event(archive_home, tmp_path) -> None:
    """A crash mid-append leaves a torn (newline-less) fragment; the next append must
    isolate it rather than gluing a valid record onto it."""
    import_cc_session(tmp_path)
    before = ta.status()["events"]
    thread_file = one_thread_file(archive_home)

    # Simulate the crash: a torn fragment with no trailing newline, and the writer's
    # cached append handle gone with the process.
    jsonl_log.reset_handles()
    with open(thread_file, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99999, "torn": tr')

    # The next commit appends a valid event through the normal staged path.
    with get_session() as s:
        tid = s.execute(select(Thread.id)).scalars().first()
        jsonl_log.write_events(s, [Event(
            thread_id=tid, stream_id="x2", event_type="user_message_sent",
            payload={"content": "survives the torn tail"}, occurred_at=_now(), dedup_key="k-torn",
        )])
        s.commit()

    # The fragment is its own line; the new record parses.
    lines = thread_file.read_text(encoding="utf-8").splitlines()
    assert lines[-2].endswith('"torn": tr')
    assert json.loads(lines[-1])["payload"]["content"] == "survives the torn tail"

    # Reindex loses only the fragment (which never committed), not the valid event.
    counts = ta.reindex()
    assert counts["parse_errors"] == 1
    assert ta.status()["events"] == before + 1
    assert ta.search("survives the torn")


# ── all-or-nothing drain ──────────────────────────────────────────────────────
def test_drain_failure_rolls_back_every_appended_record(archive_home, monkeypatch) -> None:
    """A mid-drain write failure must leave the truth file exactly as it was — no
    partial batch for a later reindex to resurrect."""
    init_db()
    with get_session() as s:
        s.add(Thread(id=1, name="t1"))
        s.commit()
        jsonl_log.write_events(s, [Event(
            thread_id=1, stream_id="s0", event_type="user_message_sent",
            payload={"content": "committed baseline"}, occurred_at=_now(), dedup_key="k0",
        )])
        s.commit()

    truth_file = one_thread_file(archive_home)
    baseline = truth_file.read_bytes()

    real_append = jsonl_log.append_line
    calls = {"n": 0}

    def _fail_second(path, rec):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated disk-full mid-batch")
        real_append(path, rec)

    monkeypatch.setattr(jsonl_log.drain, "append_line", _fail_second)

    with get_session() as s:
        jsonl_log.write_events(s, [
            Event(thread_id=1, stream_id="s1", event_type="user_message_sent",
                  payload={"content": "first of failed batch"}, occurred_at=_now(), dedup_key="k1"),
            Event(thread_id=1, stream_id="s2", event_type="user_message_sent",
                  payload={"content": "second of failed batch"}, occurred_at=_now(), dedup_key="k2"),
        ])
        with pytest.raises(OSError):
            s.commit()

    # Record 1 of the failed batch was truncated away with record 2's failure.
    assert truth_file.read_bytes() == baseline
    with get_session() as s:
        contents = [e.payload["content"] for e in s.execute(select(Event)).scalars()]
    assert contents == ["committed baseline"]


# ── parse-error classification ────────────────────────────────────────────────
def test_scan_classifies_torn_tail_vs_interior(archive_home, tmp_path):
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    corrupt_event_line(tf)  # interior damage
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "id": 99, "torn mid-wri')  # torn tail, no newline
    jsonl_log.reset_handles()

    counts = scan_truth_counts()
    assert counts["parse_errors"] == 2
    assert counts["parse_errors_interior"] == 1
    assert counts["parse_errors_torn_tail"] == 1


# ── repair: quarantine + restore-from-index ───────────────────────────────────
def test_repair_quarantines_and_restores_committed_event(archive_home, tmp_path):
    import_cc_session(tmp_path)
    before = event_count()
    tf = one_thread_file(archive_home)
    original = corrupt_event_line(tf)
    damaged_id = json.loads(original)["id"]

    assert ta.verify()["ok"] is False  # damage is visible…

    res = ta.repair()
    assert res["fragments_quarantined"] == 1
    assert res["events_restored_from_index"] == 1

    # …and repair makes verify green again without touching the index.
    assert event_count() == before
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
    assert event_count() == before


def test_repair_dry_run_touches_nothing_and_is_idempotent(archive_home, tmp_path):
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    corrupt_event_line(tf)
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
    import_cc_session(tmp_path)
    before = event_count()
    tf = one_thread_file(archive_home)
    tf.unlink()  # index ⊃ truth: the forbidden direction
    jsonl_log.reset_handles()

    res = ta.repair()
    assert res["events_restored_from_index"] == before
    assert res["thread_records_restored"] == 1
    assert tf.exists()
    recs = [json.loads(ln) for ln in tf.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["type"] == "thread"
    assert ta.verify()["ok"] is True


def test_repair_flags_restored_rows_failing_their_key_hash(archive_home, tmp_path):
    """A restore candidate whose payload no longer re-hashes to its own dedup_key
    is still restored (the index copy is the only copy left) but counted and
    sampled, so suspect content is seen rather than silently promoted."""
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)

    # Rot one index payload in place, then delete its truth line so repair's
    # containment pass restores it from the index.
    with get_session() as s:
        conn = s.connection().connection
        ev_id = conn.execute(
            "SELECT id FROM events WHERE dedup_key IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE events SET payload = ? WHERE id = ?",
            ('{"content": "rotted in the index"}', ev_id),
        )
        s.commit()
    lines = [
        ln for ln in tf.read_text(encoding="utf-8").splitlines()
        if json.loads(ln).get("id") != ev_id or json.loads(ln).get("type") != "event"
    ]
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    res = ta.repair()
    assert res["events_restored_from_index"] == 1
    assert res["restored_hash_mismatches"] == 1
    assert res["restored_hash_mismatch_sample"] == [ev_id]

    # The row IS in the truth again (preserved, not quarantined)…
    restored = [
        json.loads(ln) for ln in tf.read_text(encoding="utf-8").splitlines()
        if json.loads(ln).get("id") == ev_id and json.loads(ln).get("type") == "event"
    ]
    assert len(restored) == 1
    assert restored[0]["payload"] == {"content": "rotted in the index"}

    # …and the next hashes pass reports it red (new truth-side mismatch).
    v = ta.verify(hashes=True)
    assert v["hashes"]["truth"]["mismatched"] == 1
    assert v["ok"] is False
