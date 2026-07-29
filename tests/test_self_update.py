"""Self-update: plan gating (resolution, format bump) and apply/rollback.

The resolution half runs the real pip against a local wheel index standing in
for PyPI — real subprocess, real artifacts, real filename/marker rules, no
network. The apply half injects the mutating steps (install, smoke, migrate,
restart) rather than upgrading the venv running the suite.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from thread_archive import _update
from thread_archive._update import UpdatePlan, apply_update, plan_update

FORMAT_HOME = "thread_archive/_truth/layout.py"


def _wheel(index: Path, version: str, *, fmt: int | None = 1,
           module: str = FORMAT_HOME) -> Path:
    """A minimal but genuine wheel of ``thread-archive``: enough metadata for
    pip to resolve and copy it, and the one constant the format gate reads."""
    path = index / f"thread_archive-{version}-py3-none-any.whl"
    info = f"thread_archive-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("thread_archive/__init__.py", f'__version__ = "{version}"\n')
        if fmt is not None:
            z.writestr(module, f"TRUTH_FORMAT_VERSION = {fmt}\n")
        z.writestr(f"{info}/METADATA",
                   f"Metadata-Version: 2.1\nName: thread-archive\nVersion: {version}\n")
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test 1.0\n"
                                    "Root-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{info}/RECORD", "")
    return path


@pytest.fixture()
def index(tmp_path: Path) -> Path:
    """The index this install resolves against: the running 0.0.4 is published."""
    d = tmp_path / "index"
    d.mkdir()
    _wheel(d, "0.0.4")
    return d


@pytest.fixture()
def dest(tmp_path: Path) -> Path:
    """The scratch dir a plan downloads its candidate into."""
    d = tmp_path / "scratch"
    d.mkdir()
    return d


def _plan(index: Path, dest: Path, **kw) -> UpdatePlan:
    kw.setdefault("current_version", "0.0.4")
    kw.setdefault("local_format_version", 1)
    return plan_update(dest, pip_args=["--no-index", "--find-links", str(index)], **kw)


# ── plan ─────────────────────────────────────────────────────────────────────


def test_up_to_date_when_the_index_has_nothing_newer(index: Path, dest: Path) -> None:
    plan = _plan(index, dest)
    assert (plan.action, plan.target) == ("up-to-date", "0.0.4")


def test_updates_to_the_newest_published_release(index: Path, dest: Path) -> None:
    _wheel(index, "0.0.5")
    _wheel(index, "0.0.6")
    plan = _plan(index, dest)
    assert (plan.action, plan.target) == ("update", "0.0.6")
    assert plan.wheel is not None and plan.wheel.is_file()
    assert plan.wheel.name == "thread_archive-0.0.6-py3-none-any.whl"


def test_a_prerelease_is_not_a_candidate(index: Path, dest: Path) -> None:
    """Nothing passes ``--pre``: an rc on the index is invisible to an install
    that asked for a release, and its presence must not read as an update."""
    _wheel(index, "0.1.0rc1")
    assert _plan(index, dest).action == "up-to-date"


def test_a_version_ahead_of_the_index_is_not_downgraded(index: Path, dest: Path) -> None:
    """An install running ahead of what is published (this clone's own shape
    between releases) is up to date, never walked backwards."""
    plan = _plan(index, dest, current_version="9.9.9")
    assert (plan.action, plan.target) == ("up-to-date", "0.0.4")


def test_format_bump_blocks_without_override(index: Path, dest: Path) -> None:
    _wheel(index, "0.1.0", fmt=2)
    plan = _plan(index, dest)
    assert plan.action == "blocked"
    assert "truth-format" in plan.reason
    assert (plan.current_format, plan.target_format) == (1, 2)
    assert _plan(index, dest, allow_format_bump=True).action == "update"


def test_missing_format_declaration_blocks(index: Path, dest: Path) -> None:
    """A release whose format version can't be read is a gate, not a pass."""
    _wheel(index, "0.0.5", fmt=None)
    plan = _plan(index, dest)
    assert plan.action == "blocked"
    assert "truth-format" in plan.reason


def test_format_probe_survives_the_constant_moving(index: Path, dest: Path) -> None:
    """The anchored probe reads the constant's home module; a release that moved
    it is still readable from the rest of the wheel, so the gate holds."""
    _wheel(index, "0.0.5", fmt=1, module="thread_archive/_truth/elsewhere.py")
    assert _plan(index, dest).action == "update"


def test_unresolvable_index_blocks(tmp_path: Path, dest: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    plan = _plan(empty, dest)
    assert plan.action == "blocked"
    assert "pip download" in plan.reason


def test_unparseable_running_version_blocks(index: Path, dest: Path) -> None:
    plan = _plan(index, dest, current_version="0.0.4.dev1")
    assert plan.action == "blocked"
    assert "unparseable" in plan.reason


# ── apply ────────────────────────────────────────────────────────────────────


def _update_plan(index: Path, dest: Path) -> UpdatePlan:
    _wheel(index, "0.0.5")
    plan = _plan(index, dest)
    assert plan.action == "update"
    return plan


def test_apply_installs_the_inspected_wheel_then_restarts(
        index: Path, dest: Path) -> None:
    """What the gate read is what lands: the install argument is the very file
    the plan downloaded, not a name the index could re-resolve."""
    plan = _update_plan(index, dest)
    calls: list[str] = []
    res = apply_update(
        plan, home=None,
        install=lambda req: calls.append(f"install:{req}"),
        smoke=lambda h: calls.append("smoke"),
        restart=lambda: calls.append("restart"),
        retire=lambda h, t: calls.append(f"retire:{t}"),
    )
    assert res["ok"] and res["action"] == "updated"
    assert res["current"] == "0.0.4" and res["target"] == "0.0.5"
    assert calls == [f"install:{plan.wheel}", "smoke", "retire:0.0.5", "restart"]


def test_apply_retirement_failure_is_advisory(index: Path, dest: Path) -> None:
    """Unpinned fix-import overrides are temporary bridges to the next release,
    so the update that might carry the proper fix disables them — but a
    retirement hiccup never costs a good install."""
    def boom(home, target):
        raise RuntimeError("retirement hiccup")

    res = apply_update(_update_plan(index, dest), install=lambda req: None,
                       smoke=lambda h: None, restart=lambda: None, retire=boom)
    assert res["ok"] and res["action"] == "updated"


def test_apply_rolls_back_to_the_running_version_when_smoke_fails(
        index: Path, dest: Path) -> None:
    plan = _update_plan(index, dest)
    calls: list[str] = []

    def smoke(_h):
        raise RuntimeError("new install does not stand up")

    res = apply_update(
        plan, install=lambda req: calls.append(req), smoke=smoke,
        restart=lambda: pytest.fail("must not restart onto a rollback"),
        retire=lambda h, t: pytest.fail("must not retire on a rollback"),
    )
    assert not res["ok"] and res["action"] == "rolled-back"
    assert calls == [str(plan.wheel), "thread-archive==0.0.4"]


def test_apply_reports_failed_when_the_rollback_cannot_run(
        index: Path, dest: Path) -> None:
    """A rollback that itself fails leaves an install nobody proved — say so
    rather than reporting the softer 'rolled-back'."""
    plan = _update_plan(index, dest)

    def install(req: str) -> None:
        raise RuntimeError(f"pip install failed: {req}")

    res = apply_update(plan, install=install, smoke=lambda h: None,
                       restart=lambda: pytest.fail("must not restart"),
                       retire=lambda h, t: pytest.fail("must not retire"))
    assert not res["ok"] and res["action"] == "failed"
    assert "rollback to 0.0.4 also failed" in res["reason"]


def test_apply_format_bump_migrates_and_resmokes_before_restart(
        index: Path, dest: Path) -> None:
    _wheel(index, "0.1.0", fmt=2)
    plan = _plan(index, dest, allow_format_bump=True)
    assert plan.target_format == 2
    calls: list[str] = []

    res = apply_update(
        plan,
        install=lambda req: calls.append("install"),
        smoke=lambda h: calls.append("smoke"),
        migrate=lambda h: calls.append("migrate"),
        home_format_version=lambda h: 1,
        retire=lambda h, t: calls.append("retire"),
        restart=lambda: calls.append("restart"),
    )

    assert res["ok"] and res["migrated"]
    assert calls == ["install", "smoke", "migrate", "smoke", "retire", "restart"]


def test_apply_does_not_roll_back_after_migration_starts(
        index: Path, dest: Path) -> None:
    _wheel(index, "0.1.0", fmt=2)
    plan = _plan(index, dest, allow_format_bump=True)

    def fail_migration(_home) -> None:
        raise RuntimeError("migration stopped after swap")

    res = apply_update(
        plan, install=lambda req: None, smoke=lambda h: None,
        migrate=fail_migration, home_format_version=lambda h: 1,
        retire=lambda h, t: pytest.fail("must not retire after migration failure"),
        restart=lambda: pytest.fail("must not restart after migration failure"),
    )

    assert not res["ok"] and res["action"] == "migration-failed"
    assert not res["rolled_back"]


# ── the install shape ────────────────────────────────────────────────────────


def test_source_checkout_is_unavailable_and_still_recorded() -> None:
    """The suite runs from the clone, which is exactly the install shape that
    has no package to upgrade: the run reports unavailable, names the clone —
    and still stamps health.json, the record behind the status line and the
    viewer's health panel."""
    from thread_archive._ops.health import read_health

    res = _update.self_update()
    assert res["action"] == "unavailable" and not res["ok"]
    assert "source install" in res["reason"]
    assert str(_update.source_checkout()) in res["reason"]
    assert read_health()[_update.HEALTH_KEY]["action"] == "unavailable"


def test_a_managed_environment_names_its_own_upgrade_command(tmp_path: Path) -> None:
    """A `uv tool` / pipx environment is somebody else's to change; pip inside
    one is the wrong door even when it exists."""
    assert _update.managed_environment(tmp_path) is None
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    assert _update.managed_environment(tmp_path) == "uv tool upgrade thread-archive"

    pipx = tmp_path / "pipx-venv"
    pipx.mkdir()
    (pipx / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    assert _update.managed_environment(pipx) == "pipx upgrade thread-archive"


def test_self_update_records_a_check_from_a_packaged_install(
        index: Path, monkeypatch) -> None:
    """The packaged path end to end: real pip resolution against the index, the
    outcome stamped in health.json. Only the install *shape* is forced — the
    suite has no packaged install to run from."""
    from thread_archive._ops.health import read_health

    monkeypatch.setattr(_update, "source_checkout", lambda: None)
    res = _update.self_update(
        check_only=True, pip_args=["--no-index", "--find-links", str(index)])
    assert res["action"] in ("up-to-date", "update")
    rec = read_health()[_update.HEALTH_KEY]
    assert rec["action"] == res["action"] and rec["ok"]


# ── the default executors ────────────────────────────────────────────────
# What apply_update runs when nobody injects stubs: the real pip, the real
# installed `thread_archive` binary, real throwaway homes. No patching — each test
# drives the executor end to end and reads the outcome it promises.


@pytest.mark.integration
def test_default_install_translates_a_pip_failure(tmp_path: Path) -> None:
    """A file that isn't an installable distribution makes the real pip fail;
    the executor must surface that as the RuntimeError apply_update rolls back
    on — never a silent zero."""
    junk = tmp_path / "not-a-wheel.whl"
    junk.write_text("not a zip", encoding="utf-8")
    with pytest.raises(RuntimeError, match="pip install failed"):
        _update._default_install(str(junk), pip_args=["--no-index"])


@pytest.mark.integration
def test_default_smoke_passes_on_a_real_home_and_fails_on_a_broken_one(
        tmp_path: Path) -> None:
    """The smoke check is `thread-archive status` in a fresh process under the venv's
    real `thread_archive` entry point: green against a working home, a RuntimeError
    carrying the CLI's stderr when the home can't hold an archive."""
    home = tmp_path / "home"
    home.mkdir()
    _update._default_smoke(str(home))  # a virgin home must stand up

    broken = tmp_path / "not-a-dir"
    broken.write_text("a file where the home should be", encoding="utf-8")
    with pytest.raises(RuntimeError, match="`thread-archive status` under the new install"):
        _update._default_smoke(str(broken))


@pytest.mark.integration
def test_default_migrate_runs_the_real_cli_and_translates_failure(
        archive_home, tmp_path: Path, monkeypatch) -> None:
    import os
    import subprocess
    import sys

    from .helpers import cc_assistant, cc_user, write_jsonl

    # A home with real truth migrates clean. The seed import runs through the
    # same CLI the executor drives — its maintain pass writes the manifest.
    f = tmp_path / "sess.jsonl"
    write_jsonl(f, [cc_user("m"), cc_assistant("m")])
    bin_ = Path(sys.executable).with_name("thread_archive")
    r = subprocess.run(
        [str(bin_), "import", str(f)], capture_output=True, text=True,
        env={**os.environ, "THREAD_ARCHIVE_HOME": str(archive_home)},
    )
    assert r.returncode == 0, r.stderr
    _update._default_migrate(str(archive_home))

    # An empty home has no manifest to migrate: the CLI's refusal must come
    # back as the RuntimeError that stops the update (no --home flag either —
    # the executor resolves through the environment when none is given).
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(empty))
    with pytest.raises(RuntimeError, match="failed:"):
        _update._default_migrate(None)


def test_default_home_format_version_reads_the_manifest(archive_home) -> None:
    from thread_archive._store import init_db
    from thread_archive._truth.layout import TRUTH_FORMAT_VERSION

    init_db()
    assert _update._default_home_format_version(str(archive_home)) == TRUTH_FORMAT_VERSION


def test_default_retire_runs_real_retirement(archive_home) -> None:
    """Delegates to the real retire_patches over the home's config: seed one
    stale unpinned patch and watch the executor disable it."""
    from thread_archive._config import load_config, save_config

    cfg = load_config()
    cfg.setdefault("providers", {})["codex"] = {
        "module": "patch_codex:PROVIDER", "path": "/plugins/codex",
        "enabled": True, "patch": {"built_against": "0.0.1", "pinned": False},
    }
    save_config(cfg)
    _update._default_retire(None, "9.9.9")
    assert load_config()["providers"]["codex"]["enabled"] is False


def test_default_restart_is_quiet_with_no_agents_installed() -> None:
    """In the sandboxed test $HOME no launchd plists exist, so the restart is
    a no-op — and per its contract it must never raise regardless."""
    _update._default_restart()
