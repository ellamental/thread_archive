"""Self-update: plan gating (soak, dirty tree, divergence, format bump),
apply/rollback, and the watcher-side spawn gate — against real throwaway git
repos, with the mutating steps (pip, smoke, restarts) stubbed."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from thread_archive import _update
from thread_archive._update import (
    UpdatePlan,
    apply_update,
    check_due,
    maybe_spawn_self_update,
    plan_update,
)

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


def test_soak_window_holds_young_tag_back(repo: Path) -> None:
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    fresh = {"GIT_COMMITTER_DATE": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _tag(repo, "v0.0.5", env=fresh)
    plan = _plan(repo, min_age_hours=48)
    assert plan.action == "up-to-date"
    assert any("soak window" in s for s in plan.skipped)
    # A zero-hour window applies it immediately.
    assert _plan(repo, min_age_hours=0).action == "update"


def test_soak_skips_young_but_takes_matured_older(repo: Path) -> None:
    (repo / "f").write_text("1")
    _commit(repo, "v0.0.5")
    _tag(repo, "v0.0.5")  # old → eligible
    (repo / "f").write_text("2")
    _commit(repo, "v0.0.6")
    _tag(repo, "v0.0.6", env={"GIT_COMMITTER_DATE": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _git(repo, "checkout", "-q", "v0.0.4")  # the consumer sits at their installed release
    plan = _plan(repo, min_age_hours=48)
    assert (plan.action, plan.tag) == ("update", "v0.0.5")
    assert any(s.startswith("v0.0.6") for s in plan.skipped)


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


def test_self_update_records_health(repo: Path, monkeypatch) -> None:
    """The orchestrator stamps health.json (the status line + the spawner's
    once-per-interval gate) — and --check deliberately doesn't."""
    from thread_archive._ops.health import read_health

    monkeypatch.setattr(_update, "install_repo", lambda: repo)
    monkeypatch.setattr(_update, "plan_update",
                        lambda *a, **k: _plan(repo))  # skip the real fetch
    res = _update.self_update(check_only=True)
    assert res["action"] == "up-to-date"
    assert _update.HEALTH_KEY not in read_health()

    res = _update.self_update()
    rec = read_health()[_update.HEALTH_KEY]
    assert rec["action"] == "up-to-date" and rec["ok"]


def test_self_update_unavailable_without_clone(monkeypatch) -> None:
    monkeypatch.setattr(_update, "install_repo", lambda: None)
    res = _update.self_update()
    assert res["action"] == "unavailable" and not res["ok"]


# ── the watcher's spawn gate ─────────────────────────────────────────────────


def test_check_due_reads_health_stamp(monkeypatch) -> None:
    from thread_archive._ops.health import clear_health, record_health

    clear_health(_update.HEALTH_KEY)
    assert check_due()
    record_health(_update.HEALTH_KEY, {"ok": True, "action": "up-to-date"})
    assert not check_due()


def test_maybe_spawn_respects_config_and_install(monkeypatch, tmp_path) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(_update.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd))

    # Disabled → no spawn, whatever else holds.
    monkeypatch.setattr(_update, "update_config", lambda home=None: {"enabled": False})
    assert not maybe_spawn_self_update()

    # Enabled but not a clone install → no spawn.
    monkeypatch.setattr(_update, "update_config", lambda home=None: {})
    monkeypatch.setattr(_update, "install_repo", lambda: None)
    assert not maybe_spawn_self_update()

    # Enabled, clone, due → spawn (detached invocation of the console script).
    monkeypatch.setattr(_update, "install_repo", lambda: tmp_path)
    monkeypatch.setattr(_update, "check_due", lambda home=None: True)
    assert maybe_spawn_self_update()
    assert spawned and spawned[0][1] == "self-update"

    # Not due → no second spawn.
    monkeypatch.setattr(_update, "check_due", lambda home=None: False)
    assert not maybe_spawn_self_update()
    assert len(spawned) == 1
