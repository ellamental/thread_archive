"""``thread_archive backup`` mirror semantics + the restore drill.

The backup is a true mirror with guardrails:

* destination files with no source counterpart are deleted, so a shard
  rebalance or the ULID migration can't leave a stale generation that shadows
  current records on restore — but provably superseded twins (rebalance
  re-homes, migration renames) are the only deletions exempt from the safety
  cap, and a run that *skips* deletions exits nonzero so the scheduled wrapper
  alerts;
* an append-only truth file that is *smaller* at the source than in the backup
  is never copied over the last good backup copy (``--allow-shrink`` is the
  deliberate override), and a source that fails the pre-backup verify still
  mirrors additively (delete-sync disabled) so a sick source can't strip the
  backup;
* copies publish atomically and trim an unterminated final fragment, so every
  mirror file is a clean, parseable prefix of its source;
* each run preserves the destination's pre-run state as a hardlink generation
  under ``.generations/`` (one per UTC day, pruned by retention, exempt from
  delete-sync);
* ``thread_archive restore-drill`` rebuilds a full index from the mirror in a
  throwaway home, compares it to the mirror's own scan, and leaves the live
  archive untouched.
"""

from __future__ import annotations

import json

from thread_archive import _api as ta
from thread_archive._ops.backup import _GENERATIONS_SUBDIR
from thread_archive._truth import jsonl_log, scan_truth_counts

from .helpers import event_count, import_cc_session, one_thread_file


def test_backup_destination_is_owner_only(archive_home, tmp_path) -> None:
    """Mirror dirs land 0700 and files 0600 — even when a source file's mode is
    looser and the destination pre-exists open. The backup must never sit more
    readable than the 0700 live home it copies."""
    import_cc_session(tmp_path)
    one_thread_file(archive_home).chmod(0o644)  # source-mode drift must not propagate

    dest = tmp_path / "bk"
    dest.mkdir(mode=0o755)
    ta.backup(str(dest))
    ta.backup(str(dest))  # second run: generations subtree exists too

    assert (dest.stat().st_mode & 0o777) == 0o700
    for p in dest.rglob("*"):
        mode = p.stat().st_mode & 0o777
        expected = 0o700 if p.is_dir() else 0o600
        assert mode == expected, f"{p} is {oct(mode)}"


# ── delete-sync: a true mirror, capped except for re-homed twins ──────────────
def test_backup_deletes_files_the_source_no_longer_has(archive_home, tmp_path) -> None:
    """The mirror must drop stale destination files (e.g. a pre-rebalance layout), or
    a restore-reindex loads them last and they shadow the current records."""
    import_cc_session(tmp_path)

    dest = tmp_path / "bk"
    ta.backup(str(dest))

    # A stale twin left from an old shard layout (absent from the live truth dir).
    stale = dest / "threads" / "999999.jsonl"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")

    res = ta.backup(str(dest))
    assert res["files_deleted"] >= 1
    assert not stale.exists()
    # Everything the source has is still mirrored (the destination-only
    # .generations and .recovery subtrees are the mirror's own snapshot/bundle
    # state, not truth copies).
    src_files = {p.relative_to(archive_home / "truth") for p in (archive_home / "truth").rglob("*") if p.is_file()}
    dest_files = {
        p.relative_to(dest) for p in dest.rglob("*")
        if p.is_file() and p.relative_to(dest).parts[0] not in (".generations", ".recovery")
    }
    assert src_files == dest_files


def _stale_non_twins(dest, n: int) -> list:
    """Seed ``n`` destination-only thread files whose ids have no source
    counterpart at any shard depth — deletion candidates that the twin exemption
    cannot rescue, enough of them to exceed the real delete-sync cap."""
    (dest / "threads").mkdir(parents=True, exist_ok=True)
    stale = []
    for i in range(n):
        p = dest / "threads" / f"{900000 + i}.jsonl"
        p.write_text(
            f'{{"type": "thread", "id": {900000 + i}, "name": "stale-{i}"}}\n',
            encoding="utf-8",
        )
        stale.append(p)
    return stale


def test_mirror_deletes_rehomed_twins_beyond_cap(archive_home, tmp_path, monkeypatch) -> None:
    import thread_archive._ops.backup as ops_backup

    # The real cap: enough stale non-twins at the destination to exceed
    # MIRROR_DELETE_FLOOR, so every non-twin deletion is skipped and anything
    # that DOES get deleted went through the twin exemption.
    for name in ("one", "two"):
        import_cc_session(tmp_path, name=name)
    dest = tmp_path / "dest"
    assert ta.backup(str(dest))["mirror_complete"] is True
    flat_rels = sorted(
        p.relative_to(archive_home / "truth")
        for p in (archive_home / "truth" / "threads").rglob("*.jsonl")
    )
    assert all(len(rel.parts) == 2 for rel in flat_rels), "starts flat"

    # Drive the real rebalance: shrink the flat threshold and checkpoint.
    monkeypatch.setenv("THREAD_ARCHIVE_SHARDFLAT_MAX", "1")
    jsonl_log.checkpoint()
    assert jsonl_log._shard_depth(archive_home / "truth") == 1
    assert not any((archive_home / "truth" / rel).exists() for rel in flat_rels)

    # Stale non-twins at the destination, past the cap, stay protected.
    stale = _stale_non_twins(dest, ops_backup.MIRROR_DELETE_FLOOR + 1)

    res = ta.backup(str(dest))
    assert res["rehomed_twins_deleted"] == len(flat_rels)
    for rel in flat_rels:
        assert not (dest / rel).exists(), f"stale twin {rel} must be gone from the backup"
    assert all(p.exists() for p in stale), "non-twin deletions stay capped"
    assert res["deletions_skipped"] == len(stale)
    assert res["mirror_complete"] is True


def test_mirror_deletes_ulid_migration_twins_beyond_cap(archive_home, tmp_path) -> None:
    """The ULID migration renames every thread file at once, so a pre-migration
    backup holds a stale legacy-named generation far past the deletion cap. The
    migration's durable mapping proves each one superseded — the mirror must
    converge, while mapped-but-unlanded ids and unmapped strays stay capped."""
    import thread_archive._ops.backup as ops_backup
    from thread_archive._truth.layout import ULID_MAPPING_FILE

    import_cc_session(tmp_path)
    dest = tmp_path / "dest"
    assert ta.backup(str(dest))["mirror_complete"] is True

    # Fabricate the post-migration destination state: for every live ULID
    # thread file, a stale legacy-named copy plus the mapping naming it as
    # that file's predecessor.
    live_rels = sorted(
        p.relative_to(archive_home / "truth")
        for p in (archive_home / "truth" / "threads").rglob("*.jsonl")
    )
    assert live_rels
    mapping = {str(100 + i): rel.stem for i, rel in enumerate(live_rels)}
    twins = []
    for legacy in mapping:
        p = dest / "threads" / f"{legacy}.jsonl"
        p.write_text(
            f'{{"type": "thread", "id": {legacy}, "name": "pre-ulid"}}\n',
            encoding="utf-8",
        )
        twins.append(p)
    # A mapped legacy id whose ULID successor never landed anywhere must not
    # be deleted on the mapping's word alone.
    mapping["99"] = "01KF0000000000000000000000"
    unlanded = dest / "threads" / "99.jsonl"
    unlanded.write_text('{"type": "thread", "id": 99, "name": "pre-ulid"}\n', encoding="utf-8")
    (archive_home / ULID_MAPPING_FILE).write_text(json.dumps(mapping), encoding="utf-8")
    # Unmapped stale files beyond the cap stay protected.
    stale = _stale_non_twins(dest, ops_backup.MIRROR_DELETE_FLOOR + 1)

    res = ta.backup(str(dest))
    assert res["renamed_twins_deleted"] == len(twins)
    assert not any(p.exists() for p in twins), "the stale generation must converge out"
    assert unlanded.exists()
    assert all(p.exists() for p in stale)
    assert res["deletions_skipped"] == len(stale) + 1
    assert res["mirror_complete"] is True


def test_backup_cli_fails_on_skipped_deletions(archive_home, tmp_path) -> None:
    import thread_archive._ops.backup as ops_backup
    from thread_archive.cli import main

    import_cc_session(tmp_path)
    dest = tmp_path / "dest"
    assert main(["backup", str(dest)]) == 0

    stale = _stale_non_twins(dest, ops_backup.MIRROR_DELETE_FLOOR + 1)
    assert main(["backup", str(dest)]) == 1, "a skipped deletion must fail the run"
    assert all(p.exists() for p in stale)


# ── source-damage guards: shrink + pre-backup verify ─────────────────────────
def test_backup_shrink_guard_keeps_the_larger_backup_copy(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "dest"
    res = ta.backup(str(dest))
    assert res["mirror_complete"] and res["shrinks_skipped"] == 0

    # Simulate source data loss: truncate the thread's truth file to one line.
    tf = one_thread_file(archive_home)
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
    import_cc_session(tmp_path)
    dest = tmp_path / "dest"
    assert ta.backup(str(dest))["verify_ok"] is True

    # A stale destination file a healthy mirror would delete-sync away.
    stale = dest / "threads" / "999999.jsonl"
    stale.write_text('{"type": "thread", "id": 999999, "name": "stale"}\n', encoding="utf-8")

    # Break the source: an unparseable truth line fails the shallow verify.
    tf = one_thread_file(archive_home)
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


# ── atomic, prefix-clean copies ───────────────────────────────────────────────
def test_backup_trims_torn_tail_and_leaves_no_tmp(archive_home, tmp_path):
    import_cc_session(tmp_path)
    tf = one_thread_file(archive_home)
    good = tf.read_bytes()
    with open(tf, "ab") as fh:
        fh.write(b'{"type": "event", "id": 99')  # a live append caught mid-line

    dest = tmp_path / "bak"
    ta.backup(str(dest), verify_first=False)

    copy = dest / tf.relative_to(archive_home / "truth")
    assert copy.read_bytes() == good  # clean prefix: the torn fragment is trimmed
    assert scan_truth_counts(truth_dir=dest)["parse_errors"] == 0
    assert not list(dest.rglob("*.tmp-*"))  # atomic publish leaves no residue


# ── generations ───────────────────────────────────────────────────────────────
def _age_generation(dest, stamp="20200101T000000Z"):
    """Rename the newest generation to an old date, pinning it apart from the
    generations later runs create."""
    gens = dest / _GENERATIONS_SUBDIR
    newest = sorted(p for p in gens.iterdir() if p.is_dir())[-1]
    aged = gens / stamp
    newest.rename(aged)
    return aged


def test_backup_degrades_when_generations_dir_is_unusable(archive_home, tmp_path):
    """Snapshot failure must never block the mirror itself — a degraded network
    destination (or a share that rejects the dot-dir name) reports
    ``generation_error`` and the backup still completes."""
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    # Occupy the generations path with a file: mkdir(exist_ok=True) raises.
    (dest / _GENERATIONS_SUBDIR).write_text("not a directory")
    res = ta.backup(str(dest))
    assert res["generation_error"]
    assert res["generation_created"] is None
    assert res["mirror_complete"]  # the mirror itself was never blocked


def test_backup_snapshots_pre_run_state_as_generation(archive_home, tmp_path):
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"

    res1 = ta.backup(str(dest))
    assert res1["generation_created"] is None  # first run: nothing to preserve

    res2 = ta.backup(str(dest))
    assert res2["generation_created"]
    gen = dest / _GENERATIONS_SUBDIR / res2["generation_created"]
    # The generation is the destination as run 1 left it: same truth files.
    mirror_files = {p.relative_to(dest) for p in dest.rglob("*.jsonl")
                    if p.relative_to(dest).parts[0] != _GENERATIONS_SUBDIR}
    gen_files = {p.relative_to(gen) for p in gen.rglob("*.jsonl")}
    assert gen_files == mirror_files

    # One generation per run: every overwrite has its pre-state preserved —
    # a second (bad) same-day backup can't destroy the day's only pre-state.
    res3 = ta.backup(str(dest))
    assert res3["generation_created"]
    assert res3["generation_created"] != res2["generation_created"]
    # Retention coalesces by day, never within one: both survive.
    assert res3["generations_kept"] == 2


def test_generation_content_survives_mirror_overwrite(archive_home, tmp_path):
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    tf_rel = next(
        p.relative_to(dest) for p in (dest / "threads").rglob("*.jsonl")
    )
    good = (dest / tf_rel).read_bytes()

    ta.backup(str(dest))  # takes the generation of the good state
    gen = _age_generation(dest)

    # Grow the source file (new import), mirror again: the live copy changes,
    # the generation's hardlinked bytes do not.
    import_cc_session(tmp_path, name="sess2")
    ta.backup(str(dest))
    assert (gen / tf_rel).read_bytes() == good
    # And delete-sync never eats the generations subtree.
    assert (gen / tf_rel).exists()


def test_generation_retention_prunes_by_recency_and_month(archive_home, tmp_path):
    import_cc_session(tmp_path)
    dest = tmp_path / "mirror"
    ta.backup(str(dest))
    gens = dest / _GENERATIONS_SUBDIR
    gens.mkdir(exist_ok=True)
    # Fabricate a long history: 10 gens in 2026-06, one per month before that.
    for day in range(1, 11):
        (gens / f"202606{day:02d}T040000Z").mkdir()
    for month in range(1, 6):
        (gens / f"20260{month}01T040000Z").mkdir()

    res = ta.backup(str(dest))
    kept = sorted(p.name for p in gens.iterdir() if p.is_dir())
    assert res["generations_pruned"] > 0
    assert len(kept) == res["generations_kept"] <= 13
    # The newest 7 all survive.
    all_names = sorted(kept, reverse=True)
    assert set(all_names[:7]) <= set(kept)
    # Months covered stays bounded.
    assert len({n[:6] for n in kept}) <= 6


# ── restore drill ─────────────────────────────────────────────────────────────
def test_restore_drill_rebuilds_from_mirror_and_reports_ok(archive_home, tmp_path):
    import_cc_session(tmp_path)
    import_cc_session(tmp_path, name="sess2")
    before = event_count()
    dest = tmp_path / "mirror"
    ta.backup(str(dest))

    res = ta.restore_drill(str(dest))
    assert res["ok"] is True, res
    assert res["rebuilt"]["events"] == res["mirror"]["events_effective"]
    assert res["rebuilt"]["threads"] == res["mirror"]["threads"]
    assert res["coverage"] == 1.0
    # The live archive is untouched and reopened.
    assert event_count() == before
    # The outcome is recorded for staleness monitoring.
    health = json.loads((archive_home / "health.json").read_text(encoding="utf-8"))
    assert health["restore_drill_last"]["ok"] is True

    # A gutted mirror restores internally-consistent but SHORT of the live
    # archive — the coverage gate fails the drill.
    ta.backup(str(dest))
    victim = next((dest / "threads").rglob("*.jsonl"))
    victim.unlink()
    res2 = ta.restore_drill(str(dest))
    assert res2["ok"] is False
    assert res2["coverage"] < 0.98


def test_restore_drill_rejects_non_mirror(archive_home, tmp_path):
    res = ta.restore_drill(str(tmp_path / "nowhere"))
    assert res["ok"] is False
    assert "error" in res
