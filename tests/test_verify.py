"""``archive verify`` — every facet of the truth↔index integrity check.

* shallow: parse-scan + count parity, ``PRAGMA quick_check`` on the index, and
  FTS shadow↔FTS5 parity;
* ``deep=True``: per-event dedup_key parity, coverage re-extract (splitting
  genuinely-unindexed from empty-extract), and thread-metadata drift
  (report-only);
* ``hashes=True``: re-hash stored payloads against the content hash embedded in
  their own dedup_key on both stores, persist a baseline in the manifest and
  report the delta, and upgrade the index self-check to the full
  ``integrity_check``;
* ``backup=DEST``: parse-scan (and with hashes, hash-scan) a mirror;
* every run records its outcome where ``status`` surfaces it.
"""

from __future__ import annotations

import json

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log

from .helpers import corrupt_event_line, import_cc_session, one_thread_file


# ── shallow ───────────────────────────────────────────────────────────────────
def test_verify_reports_index_quick_check(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    res = ta.verify()
    assert res["index"]["quick_check"] == "ok"
    assert res["ok"] is True


def test_shallow_verify_fails_on_fts5_drift(archive_home, tmp_path):
    import_cc_session(tmp_path)
    assert ta.verify()["ok"] is True
    with get_session() as s:
        s.execute(text("DELETE FROM event_search WHERE rowid IN "
                       "(SELECT rowid FROM event_search LIMIT 1)"))
        s.commit()
    v = ta.verify()
    assert v["ok"] is False
    assert v["fts"]["fts5_rows"] == v["fts"]["shadow_rows"] - 1


# ── deep ──────────────────────────────────────────────────────────────────────
def test_deep_verify_flags_dedup_key_mismatch(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    assert ta.verify(deep=True)["ok"] is True

    with get_session() as s:
        s.execute(text(
            "UPDATE events SET dedup_key = 'tampered:key:blk=0:deadbeefdeadbeef' "
            "WHERE id = (SELECT min(id) FROM events WHERE dedup_key IS NOT NULL)"
        ))
        s.commit()

    res = ta.verify(deep=True)
    assert res["ok"] is False
    assert res["deep"]["events_key_mismatch"] == 1
    assert len(res["deep"]["key_mismatch_sample"]) == 1


def test_deep_verify_splits_unindexed_from_empty_extract(archive_home, tmp_path):
    import_cc_session(tmp_path)
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


def test_deep_verify_reports_thread_metadata_drift(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    assert ta.verify(deep=True)["deep"]["thread_meta_mismatch"] == 0

    # An index-only title mutation the truth never received.
    with get_session() as s:
        s.execute(text("UPDATE threads SET title = 'tampered title'"))
        s.commit()

    res = ta.verify(deep=True)
    assert res["deep"]["thread_meta_mismatch"] == 1
    assert res["deep"]["thread_meta_sample"]
    assert res["ok"] is True, "metadata drift is report-only"


# ── hashes ────────────────────────────────────────────────────────────────────
def test_verify_hashes_detects_payload_corruption_both_sides(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    base = ta.verify(hashes=True)["hashes"]
    assert base["truth"]["checked"] > 0 and base["truth"]["mismatched"] == 0
    assert base["index"]["checked"] > 0 and base["index"]["mismatched"] == 0

    # Index-side corruption: rewrite one payload without touching its key.
    with get_session() as s:
        s.execute(text(
            "UPDATE events SET payload = '{\"content\": \"tampered\"}' "
            "WHERE id = (SELECT min(id) FROM events WHERE dedup_key IS NOT NULL)"
        ))
        s.commit()
    # Truth-side corruption: flip a payload in the highest-id event's truth line.
    tf = one_thread_file(archive_home)
    lines = tf.read_text(encoding="utf-8").splitlines()
    events = [(i, json.loads(ln)) for i, ln in enumerate(lines)
              if '"type": "event"' in ln]
    idx, rec = next((i, r) for i, r in reversed(events) if r.get("dedup_key"))
    rec["payload"] = {"content": "rotted"}
    lines[idx] = json.dumps(rec)
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    res = ta.verify(hashes=True)["hashes"]
    assert res["index"]["mismatched"] == 1
    assert res["truth"]["mismatched"] == 1
    assert res["index"]["mismatch_sample"] != res["truth"]["mismatch_sample"]


def test_verify_hashes_persists_baseline_and_reports_delta(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    first = ta.verify(hashes=True)["hashes"]
    assert "previous" not in first

    second = ta.verify(hashes=True)["hashes"]
    assert second["previous"]["truth_mismatched"] == 0
    assert second["delta"] == {"truth_mismatched": 0, "index_mismatched": 0}


# ── backup mirror checks ──────────────────────────────────────────────────────
def test_verify_backup_scans_the_mirror(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "dest"
    ta.backup(str(dest))

    res = ta.verify(backup=str(dest))
    assert res["backup"]["ok"] is True
    assert res["backup"]["scan"]["parse_errors"] == 0
    assert res["backup"]["coverage"] == 1.0
    assert res["ok"] is True

    # A corrupted mirror fails the backup check (and overall ok).
    bf = next((dest / "threads").rglob("*.jsonl"))
    with open(bf, "a", encoding="utf-8") as fh:
        fh.write("{rot\n")
    res = ta.verify(backup=str(dest))
    assert res["backup"]["ok"] is False
    assert res["ok"] is False

    # A directory that isn't a truth mirror is refused, not counted as empty-and-fine.
    res = ta.verify(backup=str(tmp_path / "not-a-mirror"))
    assert res["backup"]["ok"] is False
    assert "error" in res["backup"]


def test_verify_hashes_covers_backup_mirror(archive_home, tmp_path):
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    res = ta.verify(hashes=True, backup=str(dest))
    assert res["index"]["check"] == "integrity_check"
    for side in ("truth", "index"):
        assert "no_key" in res["hashes"][side]
    bh = res["backup"]["hashes"]
    assert bh["checked"] > 0 and bh["mismatched"] == 0

    # Rot a payload in the mirror (valid JSON, changed content): the parse scan
    # stays green, the hash scan sees it.
    tf = next((dest / "threads").rglob("*.jsonl"))
    lines = tf.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        rec = json.loads(ln)
        if rec.get("type") == "event" and rec.get("dedup_key") and "content" in rec.get("payload", {}):
            rec["payload"]["content"] = "bitrot changed this"
            lines[i] = json.dumps(rec)
            break
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res2 = ta.verify(hashes=True, backup=str(dest))
    assert res2["backup"]["ok"] is True  # parse-and-count alone can't see it…
    assert res2["backup"]["hashes"]["mismatched"] >= 1  # …the hash scan can

    # Without --hashes the daily check stays on quick_check.
    assert ta.verify()["index"]["check"] == "quick_check"


# ── operational health records ────────────────────────────────────────────────
def test_verify_and_backup_record_outcomes_in_status(archive_home, tmp_path):
    import_cc_session(tmp_path)
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
    tf = one_thread_file(archive_home)
    corrupt_event_line(tf)
    assert ta.verify()["ok"] is False
    assert ta.status()["last_verify"]["ok"] is False
