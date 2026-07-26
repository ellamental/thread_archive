"""The backup's recovery bundle: ``<dest>/.recovery/`` makes a destination
restore the *install*, not just the conversations.

The bundle carries the operator's choices (``config.json``), the retained
original exports (``dumps/imported/``), and reference snapshots of
``health.json`` + the operational ledgers. Its load-bearing properties:

* head-only — never snapshotted into ``.generations``, so the bundle tracks the
  install's current state instead of accumulating superseded copies in dated
  snapshots;
* delete-synced against the live home under the same gate as the truth mirror,
  so a superseded export or a removed config actually leaves the destination;
* invisible to the truth machinery — the drill and restore rebuild identical
  counts with the bundle present;
* ``restore`` installs choices + recovery material only; the health/ledger
  snapshots stay at the mirror (a restored home must not claim the source
  install's operational history).
"""

from __future__ import annotations

import json

from thread_archive import _api as ta
from thread_archive._ops.backup import _GENERATIONS_SUBDIR, _RECOVERY_SUBDIR

from .helpers import import_cc_session

_CONFIG = {"sources": {"grok": {"enabled": False}}}


def _seed_home_extras(home) -> None:
    """Give the home every kind of bundle member: config, a retained export, and
    an operational ledger."""
    (home / "config.json").write_text(json.dumps(_CONFIG), encoding="utf-8")
    exp = home / "dumps" / "imported" / "chatgpt"
    exp.mkdir(parents=True)
    (exp / "export.zip").write_bytes(b"PK\x03\x04 not a real zip")
    (home / "validation-drift.jsonl").write_text(
        '{"at": "2026-07-01T00:00:00+00:00", "count": 1}\n', encoding="utf-8"
    )


def test_backup_syncs_the_recovery_bundle(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    _seed_home_extras(archive_home)

    dest = tmp_path / "bk"
    res = ta.backup(str(dest))

    assert res["bundle_copied"] >= 3
    rec = dest / _RECOVERY_SUBDIR
    assert json.loads((rec / "config.json").read_text(encoding="utf-8")) == _CONFIG
    assert (rec / "dumps" / "imported" / "chatgpt" / "export.zip").read_bytes().startswith(b"PK")
    assert (rec / "validation-drift.jsonl").is_file()
    # The pre-backup verify already recorded health, so the snapshot rides too.
    assert (rec / "health.json").is_file()
    # A second run with nothing changed recopies at most the health snapshot
    # (each run's own verify records into it) — the bundle is an incremental
    # mirror, not a full recopy, and deletes nothing that still exists.
    res2 = ta.backup(str(dest))
    assert res2["bundle_copied"] <= 1 and res2["bundle_deleted"] == 0


def test_bundle_is_head_only_and_removal_propagates(archive_home, tmp_path) -> None:
    """No generation ever holds bundle files, and a member deleted at the home
    leaves the destination on the next run — the bundle mirrors the install's
    current state rather than accumulating beside it."""
    import_cc_session(tmp_path)
    _seed_home_extras(archive_home)

    dest = tmp_path / "bk"
    ta.backup(str(dest))
    ta.backup(str(dest))  # snapshots run 1's dest state into .generations

    gens = dest / _GENERATIONS_SUBDIR
    assert gens.is_dir()
    assert not [p for p in gens.rglob("*") if p.name in ("config.json", "validation-drift.jsonl")]

    (archive_home / "validation-drift.jsonl").unlink()
    res = ta.backup(str(dest))
    assert res["bundle_deleted"] >= 1
    assert not (dest / _RECOVERY_SUBDIR / "validation-drift.jsonl").exists()
    assert not [p for p in gens.rglob("*") if p.name == "validation-drift.jsonl"]
    # The rest of the bundle is unaffected by one member leaving.
    assert (dest / _RECOVERY_SUBDIR / "config.json").is_file()


def test_restore_installs_choices_and_recovery_material_only(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    _seed_home_extras(archive_home)
    dest = tmp_path / "bk"
    ta.backup(str(dest))

    new_home = tmp_path / "recovered"
    res = ta.restore(str(dest), str(new_home))
    assert res["ok"], res
    assert res["bundle"] == {"config": True, "retained_exports": 1}
    assert json.loads((new_home / "config.json").read_text(encoding="utf-8")) == _CONFIG
    assert (new_home / "dumps" / "imported" / "chatgpt" / "export.zip").is_file()
    # History stays at the mirror: the restored home's health file holds only
    # what the restore itself recorded, and no source ledger is installed.
    health = json.loads((new_home / "health.json").read_text(encoding="utf-8"))
    assert "restore_last" in health and "backup_last" not in health
    assert not (new_home / "validation-drift.jsonl").exists()


def test_restore_from_a_generation_installs_the_head_bundle(archive_home, tmp_path) -> None:
    """Generations carry no bundle by design; a generation restore still gets
    the head's (current) config."""
    import_cc_session(tmp_path)
    _seed_home_extras(archive_home)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    ta.backup(str(dest))
    gens = ta.list_generations(str(dest))
    assert gens

    new_home = tmp_path / "recovered"
    res = ta.restore(str(dest), str(new_home), generation=gens[0])
    assert res["ok"], res
    assert res["bundle"]["config"] is True
    assert json.loads((new_home / "config.json").read_text(encoding="utf-8")) == _CONFIG


def test_drill_ignores_the_bundle_and_reports_it(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    _seed_home_extras(archive_home)
    dest = tmp_path / "bk"
    ta.backup(str(dest))

    res = ta.restore_drill(str(dest))
    # ok proves the bundle never entered the rebuilt truth: the drill's counts
    # match the mirror's scan exactly.
    assert res["ok"], res
    b = res["bundle"]
    assert b["present"] and b["config"] and b["health"]
    assert b["retained_exports"] == 1


def test_drill_reports_an_absent_bundle(archive_home, tmp_path) -> None:
    """A pre-bundle mirror (or a foreign copy) drills fine and says what a
    disaster would NOT get back."""
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    import shutil

    shutil.rmtree(dest / _RECOVERY_SUBDIR, ignore_errors=True)

    res = ta.restore_drill(str(dest))
    assert res["ok"], res
    assert res["bundle"]["present"] is False
