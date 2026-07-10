"""Tests for the fifth-pass integrity hardening:

1. The backup shrink guard: an append-only truth file that is *smaller* at the
   source than in the backup (truncation/corruption at the source) is never
   copied over the last good backup copy; ``--allow-shrink`` is the deliberate
   override.
2. The pre-backup verify gate: a source that fails the shallow integrity check
   still mirrors, but additively — delete-sync is disabled so a sick source
   can't strip the backup.
3. Manifest shard-depth inference: a corrupt or deleted ``manifest.json`` on a
   sharded archive infers its depth from the directory layout instead of
   silently resetting writers to the flat layout.
4. ``verify`` runs ``PRAGMA quick_check`` on the index and reports it.
5. ``verify(backup=...)`` parse-scans a backup mirror and reports coverage.
6. Deep verify reports thread-metadata drift between the winning truth record
   and the index row (report-only).
7. The maintenance checkpoint (``snapshots=False``) keeps ``import_state.jsonl``
   fresh, shrinking the watermark-regression window.
8. ``verify(hashes=True)`` persists a baseline and reports the delta on the
   next run.
9. ``ThreadEvent.occurred_at`` defaults tz-aware.
"""

from __future__ import annotations

import json

from sqlalchemy import text

import thread_archive as ta
from thread_archive.store import get_session
from thread_archive.truth import jsonl_log

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello integrity five"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back again"}]}}


def _write_cc(path, lines):
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _import_session(tmp_path, name="sess"):
    f = tmp_path / f"{name}.jsonl"
    # Distinct content per session name, or CC continuation detection merges the
    # second session into the first thread.
    user = {**USER, "uuid": f"u-{name}",
            "message": {"role": "user", "content": f"hello integrity five {name}"}}
    asst = {**ASSISTANT, "uuid": f"a-{name}"}
    _write_cc(f, [user, asst])
    ta.import_path(f)


def _one_thread_file(archive_home):
    return next((archive_home / "truth" / "threads").rglob("*.jsonl"))


# ── 1. shrink guard ───────────────────────────────────────────────────────────
def test_backup_shrink_guard_keeps_the_larger_backup_copy(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    dest = tmp_path / "dest"
    res = ta.backup(str(dest))
    assert res["mirror_complete"] and res["shrinks_skipped"] == 0

    # Simulate source data loss: truncate the thread's truth file to one line.
    tf = _one_thread_file(archive_home)
    first_line = tf.read_text(encoding="utf-8").splitlines()[0]
    tf.write_text(first_line + "\n", encoding="utf-8")
    jsonl_log.reset_handles()

    res = ta.backup(str(dest))
    assert res["shrinks_skipped"] == 1
    assert res["shrink_sample"], "the guarded file must be identified"
    assert res["mirror_complete"] is False  # divergent until investigated
    dp = dest / tf.relative_to(archive_home / "truth")
    assert dp.stat().st_size > tf.stat().st_size, "backup copy kept, not overwritten"

    # The deliberate override propagates the shrink.
    res = ta.backup(str(dest), allow_shrink=True)
    assert res["shrinks_skipped"] == 0
    assert dp.stat().st_size == tf.stat().st_size


def test_backup_verify_gate_disables_delete_sync(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    dest = tmp_path / "dest"
    assert ta.backup(str(dest))["verify_ok"] is True

    # A stale destination file a healthy mirror would delete-sync away.
    stale = dest / "threads" / "999999.jsonl"
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")

    # Break the source: an unparseable truth line fails the shallow verify.
    tf = _one_thread_file(archive_home)
    with open(tf, "a", encoding="utf-8") as fh:
        fh.write("{this is not json\n")
    jsonl_log.reset_handles()

    res = ta.backup(str(dest))
    assert res["verify_ok"] is False
    assert res["files_deleted"] == 0
    assert stale.exists(), "a sick source must not strip the backup"

    # Repair the source; the next (healthy) run delete-syncs the stale file.
    lines = tf.read_text(encoding="utf-8").splitlines()
    tf.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    jsonl_log.reset_handles()
    res = ta.backup(str(dest))
    assert res["verify_ok"] is True
    assert not stale.exists()


# ── 3. manifest depth inference ───────────────────────────────────────────────
def test_manifest_corruption_infers_shard_depth_from_layout(
    archive_home, tmp_path, monkeypatch
) -> None:
    _import_session(tmp_path, "a")
    _import_session(tmp_path, "b")
    d = archive_home / "truth"

    monkeypatch.setattr(jsonl_log, "_FLAT_MAX", 1)  # force a rebalance at 2 threads
    jsonl_log.checkpoint(snapshots=False)
    assert jsonl_log._shard_depth(d) >= 1
    depth = jsonl_log._shard_depth(d)

    # Corrupt manifest: depth must be inferred from the bucket dirs, never reset flat.
    (d / "manifest.json").write_text("{corrupt", encoding="utf-8")
    assert jsonl_log._read_manifest(d)["shard_depth"] == depth

    # Deleted manifest: same inference.
    (d / "manifest.json").unlink()
    assert jsonl_log._read_manifest(d)["shard_depth"] == depth

    # A flat archive (no bucket dirs) still infers 0.
    assert jsonl_log._infer_shard_depth(tmp_path / "nowhere") == 0


# ── 4/5. verify: quick_check + backup scan ────────────────────────────────────
def test_verify_reports_index_quick_check(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    res = ta.verify()
    assert res["index"]["quick_check"] == "ok"
    assert res["ok"] is True


def test_verify_backup_scans_the_mirror(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
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


# ── 6. thread-metadata parity (report-only) ───────────────────────────────────
def test_deep_verify_reports_thread_metadata_drift(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    assert ta.verify(deep=True)["deep"]["thread_meta_mismatch"] == 0

    # An index-only title mutation the truth never received.
    with get_session() as s:
        s.execute(text("UPDATE threads SET title = 'tampered title'"))
        s.commit()

    res = ta.verify(deep=True)
    assert res["deep"]["thread_meta_mismatch"] == 1
    assert res["deep"]["thread_meta_sample"]
    assert res["ok"] is True, "metadata drift is report-only"


# ── 7. import_state snapshot on the maintenance cadence ───────────────────────
def test_maintenance_checkpoint_snapshots_import_state(archive_home, tmp_path) -> None:
    _import_session(tmp_path)  # import_path runs checkpoint(snapshots=False)
    snap = archive_home / "truth" / "import_state.jsonl"
    assert snap.exists()
    rows = [json.loads(ln) for ln in snap.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["source"] == "claude-code"


# ── 8. hashes baseline persistence ────────────────────────────────────────────
def test_verify_hashes_persists_baseline_and_reports_delta(archive_home, tmp_path) -> None:
    _import_session(tmp_path)
    first = ta.verify(hashes=True)["hashes"]
    assert "previous" not in first

    second = ta.verify(hashes=True)["hashes"]
    assert second["previous"]["truth_mismatched"] == 0
    assert second["delta"] == {"truth_mismatched": 0, "index_mismatched": 0}


# ── 9. tz-aware occurred_at default ───────────────────────────────────────────
def test_thread_event_occurred_at_default_is_utc_aware() -> None:
    from thread_import.event_builder import ThreadEvent

    ev = ThreadEvent(event_type="x", payload={}, stream_id="s")
    assert ev.occurred_at.tzinfo is not None
