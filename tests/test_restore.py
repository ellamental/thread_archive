"""``archive restore`` — the productized recovery path: preflight refusals,
staged rebuild + verification, atomic publication with the damaged home set
aside, and generation selection. The drill proves a mirror restores; these
prove the restore itself is safe to point at a real home.
"""

from __future__ import annotations

import json

from thread_archive import _api as ta
from thread_archive._ops.backup import _GENERATIONS_SUBDIR

from .helpers import corrupt_event_line, import_cc_session


def test_restore_into_fresh_home(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))

    new_home = tmp_path / "recovered"
    res = ta.restore(str(dest), str(new_home))
    assert res["ok"], res
    assert res["rebuilt"]["events"] == res["mirror"]["events_effective"]
    assert res["smoke"]["ok"]
    # The restored home is a real, working archive: it reads and searches.
    assert ta.search("sess", home=str(new_home))
    # ...and the outcome is recorded in ITS health file.
    health = json.loads((new_home / "health.json").read_text(encoding="utf-8"))
    assert health["restore_last"]["ok"] is True
    # No staging leftovers next to the target.
    assert not list(new_home.parent.glob(f".{new_home.name}.restoring-*"))


def test_restore_refuses_nonempty_target_without_replace(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))

    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("precious", encoding="utf-8")
    res = ta.restore(str(dest), str(target))
    assert not res["ok"] and "not empty" in res["error"]
    assert (target / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_restore_replace_preserves_the_damaged_home(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))

    target = tmp_path / "damaged"
    target.mkdir()
    (target / "remnant.txt").write_text("what was left", encoding="utf-8")
    res = ta.restore(str(dest), str(target), replace=True)
    assert res["ok"], res
    # The old home moved aside intact — preserved, never deleted.
    damaged = res["damaged_home"]
    assert (tmp_path / damaged.split("/")[-1] / "remnant.txt").read_text(
        encoding="utf-8"
    ) == "what was left"
    # The target is now the restored archive.
    assert (target / "truth" / "threads").is_dir()
    assert ta.search("sess", home=str(target))


def test_restore_refuses_a_dirty_mirror_unless_allowed(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    corrupt_event_line(next((dest / "threads").rglob("*.jsonl")))

    new_home = tmp_path / "recovered"
    res = ta.restore(str(dest), str(new_home))
    assert not res["ok"] and "parse error" in res["error"]
    assert not new_home.exists()

    # The explicit override restores what parses — a flawed copy beats none.
    res = ta.restore(str(dest), str(new_home), allow_parse_errors=True)
    assert res["ok"], res
    assert res["rebuilt"]["events"] == res["mirror"]["events_effective"]


def test_restore_from_a_generation(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    before = ta.status()["events"]

    # Grow the archive and back up again: the second run snapshots the first
    # run's state as a generation before overwriting the mirror head.
    import_cc_session(tmp_path, name="later")
    ta.backup(str(dest))

    gens = ta.list_generations(str(dest))
    assert gens, "second backup run should have retained a generation"
    assert (dest / _GENERATIONS_SUBDIR / gens[0] / "threads").is_dir()

    # The head restores the grown archive; the generation restores the pre-state.
    head_home = tmp_path / "from-head"
    assert ta.restore(str(dest), str(head_home))["ok"]
    gen_home = tmp_path / "from-gen"
    res = ta.restore(str(dest), str(gen_home), generation=gens[0])
    assert res["ok"], res
    assert ta.status(home=str(gen_home))["events"] == before
    assert ta.status(home=str(head_home))["events"] > before


def test_restore_cli_list_generations(archive_home, tmp_path, capsys) -> None:
    from thread_archive.cli import main

    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    assert main(["restore", str(dest), "--list-generations"]) == 0
    assert "no generations retained" in capsys.readouterr().out


def test_restore_cli_requires_a_target(archive_home, tmp_path, capsys) -> None:
    from thread_archive.cli import main

    import_cc_session(tmp_path)
    dest = tmp_path / "bk"
    ta.backup(str(dest))
    assert main(["restore", str(dest)]) == 2
    assert "--to <home> is required" in capsys.readouterr().out
