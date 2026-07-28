"""``thread-archive index verify`` — every facet of the truth↔index integrity check.

* shallow: parse-scan + count parity (per-thread files *and* the kg log),
  ``PRAGMA quick_check`` on the index, and FTS shadow↔FTS5 parity;
* ``deep=True``: per-event dedup_key parity, kg id + content parity, coverage
  re-extract (splitting genuinely-unindexed from empty-extract), and
  thread-metadata drift (report-only);
* ``hashes=True``: re-hash stored payloads against the content hash embedded in
  their own dedup_key on both stores, cross-compare truth↔index payload
  fingerprints per id (the only check that sees rot in unkeyed payloads),
  persist a baseline in the manifest and report the delta, and upgrade the
  index self-check to the full ``integrity_check``;
* ``backup=DEST``: parse-scan (and with hashes, hash-scan) a mirror, failing on
  a mirror whose effective count dropped since the previous scan;
* a red run names its components (``failed_components``), appends its full
  result to ``verify-failures.jsonl``, and records outcomes where ``status``
  surfaces them — the escalated tiers' records carry their own verdicts.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from thread_archive import _api as ta
from thread_archive._store import get_session
from thread_archive._truth import jsonl_log

from .helpers import corrupt_event_line, import_cc_session, one_thread_file


# ── shallow ───────────────────────────────────────────────────────────────────
def test_verify_reports_index_quick_check(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    res = ta.verify()
    assert res["index"]["quick_check"] == "ok"
    assert res["ok"] is True


def test_quick_check_sees_on_disk_page_corruption(archive_home, tmp_path) -> None:
    """The index self-check judges the real file bytes.

    The pragma runs on a private, just-opened connection (a pooled connection
    can spuriously flag a healthy FTS5 index after another process's writes),
    so this pins the flip side: that private connection must be reading the
    archive's actual index file — page-level corruption on disk has to fail
    verify, not vanish into a wrong path or a cached view.
    """
    import sqlite3

    from thread_archive._config import resolve_paths

    import_cc_session(tmp_path)
    assert ta.verify()["ok"] is True
    # Target a secondary index none of verify's own queries read (counts and
    # watermarks go through primary keys; parity counts through the FTS
    # tables), so the run reaches the pragma instead of dying earlier — only
    # the page-level self-check can see this damage.
    with get_session() as s:
        rootpage = s.execute(text(
            "SELECT rootpage FROM sqlite_master "
            "WHERE name='idx_events_caused_by' AND type='index'"
        )).fetchone()[0]
        page_size = s.execute(text("PRAGMA page_size")).fetchone()[0]
    index_file = resolve_paths().index_path
    ta.close()  # drop pooled connections; the WAL checkpoints into the main file
    with open(index_file, "r+b") as f:
        f.seek((rootpage - 1) * page_size)
        f.write(b"\x00" * 32)
    res = ta.verify()
    assert res["ok"] is False
    assert "quick_check" in res["failed_components"]
    assert res["index"]["quick_check"] != "ok"
    # Repair path for a broken index is a rebuild; prove the file really is
    # unreadable at the SQLite level too, not just failing our wrapper.
    conn = sqlite3.connect(index_file)
    try:
        qc = [r[0] for r in conn.execute("PRAGMA quick_check(5)").fetchall()]
    finally:
        conn.close()
    assert qc != ["ok"]


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


_LEGACY_CREATE_FTS = (
    "CREATE VIRTUAL TABLE event_search USING fts5("
    "content, event_id UNINDEXED, thread_id UNINDEXED, event_type UNINDEXED, "
    "content_type UNINDEXED, tool_name UNINDEXED, occurred_at UNINDEXED, "
    "tokenize = 'porter unicode61')"
)


def test_legacy_event_search_swaps_dark_and_reindex_heals(archive_home, tmp_path):
    """A pre-external-content ``event_search`` is swapped for an empty
    current-shape table on open: lexical search goes dark (not wrong), shadow
    writes — including the trigger-hazard deletes — stay safe, verify names the
    state (``fts_triggers`` + ``fts_parity``), and ``rebuild_fts`` heals it."""
    from thread_archive._retrieval.fts import _TRIGGERS, rebuild_fts, search_events

    import_cc_session(tmp_path)
    assert search_events("Hello")  # baseline: populated, findable

    with get_session() as s:
        for name in _TRIGGERS:
            s.execute(text("DROP TRIGGER IF EXISTS " + name))
        s.execute(text("DROP TABLE event_search"))
        s.execute(text(_LEGACY_CREATE_FTS))
        s.commit()

    # First search after the swap: empty results, no error, and no triggers —
    # the dark window must stay triggerless so shadow deletes can't fire an
    # FTS 'delete' against postings the empty index doesn't hold.
    assert search_events("Hello") == []
    with get_session() as s:
        assert s.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'events_fts_a%'")).scalar() == 0
        # the hazard op, live during the window: a shadow delete must not raise
        s.execute(text(
            "DELETE FROM events_fts WHERE id = (SELECT min(id) FROM events_fts)"))
        s.commit()

    v = ta.verify()
    assert v["ok"] is False
    assert "fts_triggers" in v["failed_components"]
    assert "fts_parity" in v["failed_components"]

    rebuild_fts()
    assert search_events("Hello")
    v2 = ta.verify()
    assert "fts_triggers" not in v2["failed_components"]
    assert "fts_parity" not in v2["failed_components"]


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
    assert second["delta"] == {
        "truth_mismatched": 0, "index_mismatched": 0, "cross_mismatched": 0,
    }


def test_hashes_cross_store_catches_unkeyed_payload_divergence(archive_home, tmp_path) -> None:
    """An event with no dedup_key has no self-validation; cross-store parity is
    what catches its rot — the two stores are redundant copies and must agree,
    or the next reindex promotes the rotted truth copy over the good row."""
    import_cc_session(tmp_path)
    first = ta.verify(hashes=True)["hashes"]["cross"]
    assert first["compared"] > 0 and first["mismatched"] == 0

    # Strip the key and tamper the payload on the INDEX side only: both
    # key-hash scans are blind to it (nothing to validate against); only the
    # truth↔index fingerprint comparison can see the divergence.
    with get_session() as s:
        eid = s.execute(text("SELECT min(id) FROM events")).scalar()
        s.execute(text(
            "UPDATE events SET dedup_key = NULL, "
            "payload = '{\"content\": \"rotted\"}' WHERE id = :e"), {"e": eid})
        s.commit()

    v = ta.verify(hashes=True)
    assert v["hashes"]["cross"]["mismatched"] >= 1
    assert eid in v["hashes"]["cross"]["mismatch_sample"]
    assert v["hashes"]["new_mismatches"] is True
    assert "hashes" in v["failed_components"]
    assert v["ok"] is False

    # The stamped baseline absorbs it: unchanged corruption fails exactly once.
    v2 = ta.verify(hashes=True)
    assert v2["hashes"]["cross"]["mismatched"] >= 1
    assert v2["hashes"]["new_mismatches"] is False
    assert v2["ok"] is True


# ── the kg log on the daily tier ──────────────────────────────────────────────
def test_shallow_verify_scans_kg_log(archive_home, tmp_path) -> None:
    """A damaged topic-graph line fails the daily verify, not just the weekly
    deep pass — kg_events.jsonl is the topic graph's only truth."""
    import_cc_session(tmp_path)
    from .kg_seed import create_topic

    create_topic("Auth", "authentication concerns")
    v = ta.verify()
    assert v["ok"] is True
    assert v["truth"]["kg_events"] == 1
    assert v["drift"]["kg_events"] == 0

    with open(archive_home / "truth" / "kg_events.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{rot\n")
    v = ta.verify()
    assert v["ok"] is False
    assert "parse_errors" in v["failed_components"]


def test_shallow_verify_fails_on_kg_drift(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    from .kg_seed import create_topic

    create_topic("Auth")
    # Empty the log: the table holds a kg event the truth lacks — the forbidden
    # direction, previously invisible until the deep pass.
    (archive_home / "truth" / "kg_events.jsonl").write_text("", encoding="utf-8")
    v = ta.verify()
    assert v["drift"]["kg_events"] == 1
    assert "drift_kg_events" in v["failed_components"]
    assert v["ok"] is False


def test_deep_verify_flags_kg_content_mismatch(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    from .kg_seed import create_topic

    create_topic("Auth", "authentication concerns")
    assert ta.verify(deep=True)["deep"]["kg"]["content_mismatch"] == 0

    # An index-side payload mutation the log never received: id parity stays
    # green, only the content comparison can see it.
    with get_session() as s:
        s.execute(text("UPDATE kg_events SET payload = '{\"tampered\": true}'"))
        s.commit()
    v = ta.verify(deep=True)
    assert v["deep"]["kg"]["content_mismatch"] == 1
    assert v["ok"] is False
    assert "deep" in v["failed_components"]


# ── failure evidence ──────────────────────────────────────────────────────────
def test_failing_verify_names_components_and_keeps_evidence(archive_home, tmp_path) -> None:
    """A red verify must be diagnosable after the fact: the result names the
    failing components and the full result — samples included — lands in the
    failure ledger. health records booleans; without this, the cause of a red
    exists for one moment in a discarded dict."""
    import_cc_session(tmp_path)
    assert ta.verify()["failed_components"] == []
    assert not (archive_home / "verify-failures.jsonl").exists()

    corrupt_event_line(one_thread_file(archive_home))
    v = ta.verify()
    assert v["ok"] is False
    assert "parse_errors" in v["failed_components"]

    ledger = archive_home / "verify-failures.jsonl"
    assert v["failure_log"] == str(ledger)
    recs = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines()]
    assert recs[-1]["failed_components"] == v["failed_components"]
    assert recs[-1]["truth"]["parse_errors"] == v["truth"]["parse_errors"]
    assert recs[-1]["at"]
    # health carries the component list too, so `status` can say what broke.
    assert ta.status()["last_verify"]["failed"] == v["failed_components"]


def test_escalated_tier_records_carry_their_own_verdict(archive_home, tmp_path) -> None:
    """A red caused by one component must not force every expensive tier to
    re-run nightly: the deep/hashes health records carry their own tier's
    verdict, not the overall one."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    bf = next((dest / "threads").rglob("*.jsonl"))
    with open(bf, "a", encoding="utf-8") as fh:
        fh.write("{rot\n")

    v = ta.verify(deep=True, hashes=True, backup=str(dest))
    assert v["ok"] is False
    assert v["failed_components"] == ["backup"]
    health = json.loads((archive_home / "health.json").read_text(encoding="utf-8"))
    assert health["verify_last"]["ok"] is False
    assert health["verify_last"]["failed"] == ["backup"]
    assert health["verify_deep_last"]["ok"] is True
    assert health["verify_hashes_last"]["ok"] is True


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


def test_verify_backup_fails_on_effective_count_drop(archive_home, tmp_path) -> None:
    """A mirror whose effective count fell since the last scan lost content —
    the restore drill's coverage floor only sees drops ≥2% of the archive; the
    run-over-run diff sees one missing file."""
    import_cc_session(tmp_path, name="one")
    import_cc_session(tmp_path, name="two")
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    assert ta.verify(backup=str(dest))["backup"]["ok"] is True  # records the baseline

    next((dest / "threads").rglob("*.jsonl")).unlink()
    v = ta.verify(backup=str(dest))
    assert v["backup"]["ok"] is False
    drop = v["backup"]["effective_drop"]
    assert drop["current"] < drop["previous"]
    assert "backup" in v["failed_components"]

    # Recording the new count absorbs the drop — a deliberate shrink (an
    # --allow-shrink re-emit) fails exactly one run.
    assert ta.verify(backup=str(dest))["backup"]["ok"] is True


def test_verify_backup_fails_on_a_mirror_above_the_live_truth(archive_home, tmp_path) -> None:
    """A mirror holding more than the truth is holding content the archive let
    go of — a renamed file's twin, a deletion the mirror never applied. The
    surplus is what a restore rebuilds from, so it fails while the mirror can
    still be pruned rather than surfacing later as an unexplained shrink."""
    import_cc_session(tmp_path, name="one")
    import_cc_session(tmp_path, name="two")
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    assert ta.verify(backup=str(dest))["backup"]["ok"] is True

    # The pre-rename name left behind: the same events under a second stem,
    # which is two threads to a scan and two conflicting parents to a reindex.
    twin = next((dest / "threads").rglob("*.jsonl"))
    twin.with_name("legacy-42.jsonl").write_bytes(twin.read_bytes())

    v = ta.verify(backup=str(dest))
    assert v["backup"]["ok"] is False
    assert "backup" in v["failed_components"]
    excess = v["backup"]["coverage_excess"]
    assert excess["coverage"] > excess["ceiling"]
    assert excess["mirror_effective"] > excess["live_effective"]

    # Unlike the shrink guard, this one is not absorbed by recording a new
    # baseline: the surplus is still there on the next look, and so is the red.
    assert ta.verify(backup=str(dest))["backup"]["ok"] is False


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
