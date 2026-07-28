"""Self-update: plan gating (dirty tree, divergence, format bump) and
apply/rollback — against real throwaway git repos, with the mutating steps
(pip, smoke, restarts) stubbed."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from thread_archive import _update
from thread_archive._update import UpdatePlan, apply_update, plan_update

OLD = {"GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
       "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"}


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    import os

    full_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env=full_env)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, env=OLD)


def _tag(repo: Path, name: str, *, env: dict | None = None) -> None:
    _git(repo, "tag", "-a", name, "-m", name, env=env or OLD)


def _write_format(repo: Path, version: int) -> None:
    layout = repo / "src" / "thread_archive" / "_truth" / "layout.py"
    layout.parent.mkdir(parents=True, exist_ok=True)
    layout.write_text(f"TRUTH_FORMAT_VERSION = {version}\n", encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal 'clone': one tracked source file carrying the format version,
    a pyproject, and an old, matured release tag v0.0.4."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_format(repo, 1)
    _commit(repo, "v0.0.4")
    _tag(repo, "v0.0.4")
    return repo


def _plan(repo: Path, **kw) -> UpdatePlan:
    kw.setdefault("fetch", False)  # no remote in these fixtures
    kw.setdefault("current_version", "0.0.4")
    kw.setdefault("local_format_version", 1)
    return plan_update(repo, **kw)


# ── plan ─────────────────────────────────────────────────────────────────────


def test_up_to_date_when_no_newer_tag(repo: Path) -> None:
    plan = _plan(repo)
    assert plan.action == "up-to-date"


def test_updates_to_matured_newer_tag(repo: Path) -> None:
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    _tag(repo, "v0.0.5")
    plan = _plan(repo)
    assert (plan.action, plan.tag) == ("update", "v0.0.5")


def test_newest_tag_wins_however_recently_it_was_cut(repo: Path) -> None:
    """No age gate: the candidate is the highest release tag, full stop — an
    intermediate release is never a stepping stone, and a tag cut a minute ago
    is as eligible as one from last month."""
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    _tag(repo, "v0.0.5")
    (repo / "f").write_text("2")
    _commit(repo, "v0.0.6")
    _tag(repo, "v0.0.6", env={"GIT_COMMITTER_DATE": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _git(repo, "checkout", "-q", "v0.0.4")  # the consumer sits at their installed release
    assert (_plan(repo).action, _plan(repo).tag) == ("update", "v0.0.6")


def test_dirty_tree_blocks(repo: Path) -> None:
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    _tag(repo, "v0.0.5")
    (repo / "scratch.txt").write_text("uncommitted")
    plan = _plan(repo)
    assert plan.action == "blocked"
    assert "not clean" in plan.reason


def test_diverged_history_blocks(repo: Path) -> None:
    """A local commit the release line doesn't contain (the dev-clone shape)
    must never be fast-forwarded over."""
    _git(repo, "checkout", "-q", "-b", "release")
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    _tag(repo, "v0.0.5")
    _git(repo, "checkout", "-q", "-")  # back to the original branch
    (repo / "local.txt").write_text("local work")
    _commit(repo, "local-only commit")
    plan = _plan(repo)
    assert plan.action == "blocked"
    assert "diverged" in plan.reason


def test_format_bump_blocks_without_override(repo: Path) -> None:
    _write_format(repo, 2)
    _commit(repo, "v0.1.0 format bump")
    _tag(repo, "v0.1.0")
    plan = _plan(repo)
    assert plan.action == "blocked"
    assert "truth-format" in plan.reason
    assert _plan(repo, allow_format_bump=True).action == "update"


def test_missing_format_declaration_blocks(repo: Path) -> None:
    """A tag whose format version can't be read is a gate, not a pass."""
    (repo / "src" / "thread_archive" / "_truth" / "layout.py").write_text(
        "nothing here\n", encoding="utf-8")
    _commit(repo, "v0.0.5 drops the constant")
    _tag(repo, "v0.0.5")
    plan = _plan(repo)
    assert plan.action == "blocked"
    assert "truth-format" in plan.reason


def test_fetch_failure_blocks(repo: Path) -> None:
    plan = plan_update(repo, remote="no-such-remote", fetch=True,
                       current_version="0.0.4", local_format_version=1)
    assert plan.action == "blocked"
    assert "fetch" in plan.reason


# ── apply ────────────────────────────────────────────────────────────────────


def _release(repo: Path, tag: str) -> None:
    (repo / "f").write_text(tag)
    _commit(repo, tag)
    _tag(repo, tag)


def test_apply_checks_out_reinstalls_and_restarts(repo: Path) -> None:
    _release(repo, "v0.0.5")
    _git(repo, "checkout", "-q", "v0.0.4")
    calls: list[str] = []
    plan = _plan(repo)
    assert plan.action == "update"
    res = apply_update(
        repo, plan, home=None,
        reinstall=lambda r: calls.append("reinstall"),
        smoke=lambda h: calls.append("smoke"),
        restart=lambda: calls.append("restart"),
    )
    assert res["ok"] and res["action"] == "updated"
    assert calls == ["reinstall", "smoke", "restart"]
    assert _git(repo, "rev-parse", "HEAD").strip() == \
        _git(repo, "rev-parse", "v0.0.5^{commit}").strip()


def test_apply_retires_patches_after_smoke_before_restart(repo: Path) -> None:
    """Unpinned fix-import overrides are temporary bridges to the next release:
    the update that might carry the proper fix disables them — after smoke (only
    on a proven install), before restart (the reloading agents must not come
    back under stale overrides). A retirement failure is advisory."""
    _release(repo, "v0.0.5")
    _git(repo, "checkout", "-q", "v0.0.4")
    calls: list[str] = []
    res = apply_update(
        repo, _plan(repo),
        reinstall=lambda r: calls.append("reinstall"),
        smoke=lambda h: calls.append("smoke"),
        restart=lambda: calls.append("restart"),
        retire=lambda home, tag: calls.append(f"retire:{tag}"),
    )
    assert res["ok"]
    assert calls == ["reinstall", "smoke", "retire:v0.0.5", "restart"]

    def _boom(home, tag):
        raise RuntimeError("retirement hiccup")

    _release(repo, "v0.0.6")
    _git(repo, "checkout", "-q", "v0.0.5")
    res = apply_update(repo, _plan(repo, current_version="0.0.5"),
                       reinstall=lambda r: None, smoke=lambda h: None,
                       restart=lambda: None, retire=_boom)
    assert res["ok"] and res["action"] == "updated"  # advisory, never a rollback


def test_apply_rolls_back_when_smoke_fails(repo: Path) -> None:
    _release(repo, "v0.0.5")
    _git(repo, "checkout", "-q", "v0.0.4")
    prev = _git(repo, "rev-parse", "HEAD").strip()

    def smoke(_h):
        raise RuntimeError("new install does not stand up")

    plan = _plan(repo)
    res = apply_update(repo, plan, reinstall=lambda r: None, smoke=smoke,
                       restart=lambda: pytest.fail("must not restart onto a rollback"),
                       retire=lambda h, t: pytest.fail("must not retire on a rollback"))
    assert not res["ok"] and res["action"] == "rolled-back"
    assert _git(repo, "rev-parse", "HEAD").strip() == prev


def test_apply_format_bump_migrates_and_resmokes_before_restart(repo: Path) -> None:
    _write_format(repo, 2)
    _commit(repo, "v0.1.0 format bump")
    _tag(repo, "v0.1.0")
    _git(repo, "checkout", "-q", "v0.0.4")
    calls: list[str] = []
    plan = _plan(repo, allow_format_bump=True)
    assert plan.target_format == 2

    res = apply_update(
        repo, plan,
        reinstall=lambda r: calls.append("reinstall"),
        smoke=lambda h: calls.append("smoke"),
        migrate=lambda h: calls.append("migrate"),
        home_format_version=lambda h: 1,
        retire=lambda h, t: calls.append("retire"),
        restart=lambda: calls.append("restart"),
    )

    assert res["ok"] and res["migrated"]
    assert calls == ["reinstall", "smoke", "migrate", "smoke", "retire", "restart"]


def test_apply_does_not_roll_back_after_migration_starts(repo: Path) -> None:
    _write_format(repo, 2)
    _commit(repo, "v0.1.0 format bump")
    _tag(repo, "v0.1.0")
    _git(repo, "checkout", "-q", "v0.0.4")

    def fail_migration(_home) -> None:
        raise RuntimeError("migration stopped after swap")

    plan = _plan(repo, allow_format_bump=True)
    res = apply_update(
        repo, plan, reinstall=lambda r: None, smoke=lambda h: None,
        migrate=fail_migration, home_format_version=lambda h: 1,
        retire=lambda h, t: pytest.fail("must not retire after migration failure"),
        restart=lambda: pytest.fail("must not restart after migration failure"),
    )

    assert not res["ok"] and res["action"] == "migration-failed"
    assert not res["rolled_back"]
    assert _git(repo, "rev-parse", "HEAD").strip() == \
        _git(repo, "rev-parse", "v0.1.0^{commit}").strip()


def test_self_update_records_health(repo: Path, monkeypatch) -> None:
    """The orchestrator stamps health.json — the record behind the status line
    and the viewer's health panel."""
    from thread_archive._ops.health import read_health

    monkeypatch.setattr(_update, "install_repo", lambda: repo)
    monkeypatch.setattr(_update, "plan_update",
                        lambda *a, **k: _plan(repo))  # skip the real fetch
    res = _update.self_update(check_only=True)
    assert res["action"] == "up-to-date"
    rec = read_health()[_update.HEALTH_KEY]
    assert rec["action"] == "up-to-date" and rec["ok"]

    res = _update.self_update()
    rec = read_health()[_update.HEALTH_KEY]
    assert rec["action"] == "up-to-date" and rec["ok"]


def test_self_update_unavailable_without_clone(monkeypatch) -> None:
    monkeypatch.setattr(_update, "install_repo", lambda: None)
    res = _update.self_update()
    assert res["action"] == "unavailable" and not res["ok"]


# ── the default executors ────────────────────────────────────────────────
# What apply_update runs when nobody injects stubs: the real pip, the real
# installed `thread_archive` binary, real throwaway homes. No patching — each test
# drives the executor end to end and reads the outcome it promises.


@pytest.mark.integration
def test_default_reinstall_translates_a_pip_failure(tmp_path: Path) -> None:
    """A directory that isn't an installable project makes the real pip fail;
    the executor must surface that as the RuntimeError apply_update rolls back
    on — never a silent zero."""
    with pytest.raises(RuntimeError, match="pip install failed"):
        _update._default_reinstall(tmp_path)


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
    _update._default_retire(None, "v9.9.9")
    assert load_config()["providers"]["codex"]["enabled"] is False


def test_default_restart_is_quiet_with_no_agents_installed() -> None:
    """In the sandboxed test $HOME no launchd plists exist, so the restart is
    a no-op — and per its contract it must never raise regardless."""
    _update._default_restart()
