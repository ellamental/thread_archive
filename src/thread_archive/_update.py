"""Self-update from the release tags — how a fix reaches an installed clone.

The clone is the install, so an update is a ``git fetch`` + ``git checkout
<tag>`` + ``pip install -e .`` + agent restart — no registry, no installer.
This module automates exactly that, off the annotated release tags
(``vX.Y.Z``, see docs/releasing.md).

**The operator drives it.** ``thread_archive self-update`` is the whole
mechanism: nothing polls for releases, nothing applies one on its own, and no
configuration turns unattended apply on. ``--check`` plans and reports without
changing the clone. Guardrails on the explicit operation, on a machine that
holds someone's entire conversation history:

- **Tags only, never a branch head.** The candidate is the highest semver tag
  newer than the running ``__version__``. A moved tag is not followed (plain
  ``fetch --tags`` won't clobber an existing local tag).
- **Never with local work in play.** A dirty working tree, or local commits
  the release line doesn't contain (HEAD not an ancestor of the tag), blocks
  the update. A development clone is therefore never clobbered; a consumer
  clone is always clean.
- **Never across a truth-format bump without asking.** A reader refuses a truth
  directory newer than it understands (docs/format.md), so applying a release
  that bumps ``TRUTH_FORMAT_VERSION`` makes rollback a hard stop the moment
  the new code touches the store. The tag's declared format version is read
  out of the tag itself (``git grep``); if it is newer than ours — or cannot
  be determined — the update requires ``thread_archive self-update
  --allow-format-bump``.
- **Verify, then roll back.** After checkout + reinstall, the new code must
  pass a smoke check (``thread_archive status`` under the new install). On failure
  the previous commit is checked out and reinstalled — the archive keeps
  running the code that worked.

The outcome of each run is stamped in ``health.json``, which is what
``thread_archive status`` and the web viewer's health panel report. Config
rides ``config.json``::

    {"update": {"remote": "origin"}}

A wheel install (no clone) has nothing to update against and is reported as
unavailable. Trust anchor: HTTPS to the remote the user cloned from — the same
trust the install itself made; there is no signature layer.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_REMOTE = "origin"

# Network ops (fetch) get the long leash; local git is fast but not instant on
# a cold disk. pip + the smoke check get their own, longer, budgets.
_GIT_TIMEOUT = 300
_PIP_TIMEOUT = 900
_SMOKE_TIMEOUT = 300
_MIGRATE_TIMEOUT = 1800

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_FORMAT_RE = re.compile(r"TRUTH_FORMAT_VERSION\s*=\s*(\d+)")

HEALTH_KEY = "self_update_last"


@dataclass
class UpdatePlan:
    """The decision one check reached — what to do and why."""

    action: str  # "up-to-date" | "update" | "blocked"
    reason: str
    current: str
    tag: Optional[str] = None
    current_format: Optional[int] = None
    target_format: Optional[int] = None


# ── config ───────────────────────────────────────────────────────────────────


def update_config(home: Optional[str] = None) -> dict:
    from ._config import load_config

    cfg = load_config(home).get("update", {})
    return cfg if isinstance(cfg, dict) else {}


def _running_version() -> str:
    from . import __version__

    return __version__


def _local_format_version() -> int:
    from ._truth.layout import TRUTH_FORMAT_VERSION

    return TRUTH_FORMAT_VERSION


# ── the clone ────────────────────────────────────────────────────────────────


def install_repo() -> Optional[Path]:
    """The git clone this package runs from, or ``None`` when the install has
    no working tree to update (a wheel install into site-packages)."""
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def _git(
    repo: Path, *args: str, timeout: int = _GIT_TIMEOUT
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _parse_tag(tag: str) -> Optional[tuple[int, int, int]]:
    m = _TAG_RE.match(tag)
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


# Where TRUTH_FORMAT_VERSION lives. The anchored probe reads exactly this file
# so an unrelated assignment elsewhere in the tag (a test, a fixture) can never
# shadow the real constant on a safety gate.
_FORMAT_HOME = "src/thread_archive/_truth/layout.py"


def _tag_format_version(repo: Path, tag: str) -> Optional[int]:
    """The ``TRUTH_FORMAT_VERSION`` a tag's code declares, read out of the tag
    itself. Probes the constant's home module first; a whole-tag ``git grep``
    is the fallback so the check survives the module moving in a future
    release. ``None`` when it can't be found — callers treat that as a gate,
    not a pass."""
    r = _git(repo, "grep", "-h", "-E", "TRUTH_FORMAT_VERSION[[:space:]]*=", tag,
             "--", _FORMAT_HOME)
    m = _FORMAT_RE.search(r.stdout)
    if m is None:
        r = _git(repo, "grep", "-h", "-E", "TRUTH_FORMAT_VERSION[[:space:]]*=", tag,
                 "--", "*.py")
        m = _FORMAT_RE.search(r.stdout)
    return int(m.group(1)) if m else None


# ── plan ─────────────────────────────────────────────────────────────────────


def plan_update(
    repo: Path,
    *,
    remote: str = DEFAULT_REMOTE,
    allow_format_bump: bool = False,
    fetch: bool = True,
    current_version: Optional[str] = None,
    local_format_version: Optional[int] = None,
) -> UpdatePlan:
    """Decide whether — and to which tag — this clone should update.

    Pure decision, no mutation (the fetch updates refs, nothing else). The
    candidate is the **highest** release tag newer than the running version;
    it is taken only if it is reachable from HEAD (fast-forward only) and
    inside the truth-format gate. A format-gated candidate blocks rather than
    falling back to a lower tag — the operator asked for the newest release,
    and stopping short of it deserves to be said out loud.
    """
    version: str = _running_version() if current_version is None else current_version
    local_fmt: int = (
        _local_format_version() if local_format_version is None else local_format_version
    )
    current = _parse_tag(f"v{version}")
    if current is None:
        return UpdatePlan("blocked", f"unparseable running version {version!r}", version)

    if fetch:
        try:
            r = _git(repo, "fetch", "--tags", "--quiet", remote)
        except subprocess.TimeoutExpired:
            return UpdatePlan("blocked", f"fetch from {remote!r} timed out", version)
        if r.returncode != 0:
            return UpdatePlan(
                "blocked", f"fetch from {remote!r} failed: {r.stderr.strip()}",
                version,
            )

    r = _git(repo, "status", "--porcelain")
    if r.returncode != 0:
        return UpdatePlan("blocked", f"git status failed: {r.stderr.strip()}", version)
    if r.stdout.strip():
        return UpdatePlan("blocked", "working tree not clean — local work in play",
                          version)

    tags = [t for t in _git(repo, "tag", "--list", "v*").stdout.split()
            if (v := _parse_tag(t)) is not None and v > current]
    if not tags:
        return UpdatePlan("up-to-date", "no release tag newer than the running version",
                          version)

    tag = max(tags, key=lambda t: _parse_tag(t) or (0, 0, 0))
    fmt = _tag_format_version(repo, tag)
    if fmt is None:
        return UpdatePlan(
            "blocked",
            f"{tag} does not declare a readable truth-format version",
            version, tag=tag, current_format=local_fmt,
        )
    if not allow_format_bump and fmt > local_fmt:
        return UpdatePlan(
            "blocked",
            f"{tag} declares truth-format version {fmt} > local {local_fmt} — "
            "a one-way door; run `thread_archive self-update --allow-format-bump` deliberately",
            version, tag=tag, current_format=local_fmt, target_format=fmt,
        )
    r = _git(repo, "merge-base", "--is-ancestor", "HEAD", tag)
    if r.returncode != 0:
        return UpdatePlan(
            "blocked",
            f"local history diverged from the release line (HEAD is not an "
            f"ancestor of {tag})",
            version, tag=tag,
        )
    return UpdatePlan(
        "update", f"{tag} is the newest release tag", version,
        tag=tag, current_format=local_fmt, target_format=fmt,
    )


# ── apply ────────────────────────────────────────────────────────────────────


def _default_reinstall(repo: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(repo), "--quiet"],
        capture_output=True, text=True, timeout=_PIP_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pip install failed: {r.stderr.strip()[-500:]}")


def _default_smoke(home: Optional[str]) -> None:
    """The new install must stand up and read the archive: the console script
    exists, the package imports, the store opens. `thread_archive status` is exactly
    that, end to end, in a fresh process running the new code."""
    import os

    bin_ = Path(sys.executable).with_name("thread_archive")
    env = dict(os.environ)
    if home:
        env["THREAD_ARCHIVE_HOME"] = str(home)
    r = subprocess.run(
        [str(bin_), "status"], capture_output=True, text=True,
        timeout=_SMOKE_TIMEOUT, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"`thread_archive status` under the new install failed: {r.stderr.strip()[-500:]}"
        )


def _default_home_format_version(home: Optional[str]) -> int:
    from ._config import resolve_paths
    from ._truth.layout import _read_manifest

    return int(_read_manifest(resolve_paths(home).truth_dir).get("version", 1))


def _default_migrate(home: Optional[str]) -> None:
    """Run migration, rebuild, and verification in fresh target-code processes."""
    import os

    bin_ = Path(sys.executable).with_name("thread_archive")
    env = dict(os.environ)
    commands = [[str(bin_), "migrate"]]
    if home:
        env["THREAD_ARCHIVE_HOME"] = str(home)
        for command in commands:
            command.extend(["--home", str(home)])
    for command in commands:
        r = subprocess.run(
            command, capture_output=True, text=True, timeout=_MIGRATE_TIMEOUT, env=env,
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).strip()[-500:]
            raise RuntimeError(f"{' '.join(command[:3])} failed: {detail}")


def _default_restart() -> None:
    """Kick the installed *long-running* agents (watcher, MCP server) so they
    pick up the new code. The scheduled jobs — the backup, and the plugin's
    background drains — are run-to-completion processes that load fresh code at their next fire,
    and ``kickstart -k`` on one would *run it now*, off schedule. Fail-soft per
    agent — a restart hiccup must not be mistaken for a failed update (the
    code on disk is already correct)."""
    from . import _service

    if not _service.can_schedule():
        return
    for agent in ("watcher", "mcp"):
        try:
            if _service.agent_installed(agent):
                _service.restart_agent(agent)
        except Exception as e:  # noqa: BLE001 — per-agent, advisory
            logger.warning("self-update: could not restart %s agent: %s", agent, e)


def _default_retire(home: Optional[str], tag: str) -> None:
    """Disable unpinned ``thread_archive fix-import`` override patches built against a
    core older than ``tag`` — patches are temporary bridges to the next release
    by default, and pinned ones opt out (see :mod:`._repair.retire`)."""
    from ._repair import retire_patches

    retire_patches(home, target=tag)


def apply_update(
    repo: Path,
    plan: UpdatePlan,
    *,
    home: Optional[str] = None,
    reinstall: Optional[Callable[[Path], None]] = None,
    smoke: Optional[Callable[[Optional[str]], None]] = None,
    restart: Optional[Callable[[], None]] = None,
    retire: Optional[Callable[[Optional[str], str], None]] = None,
    migrate: Optional[Callable[[Optional[str]], None]] = None,
    home_format_version: Optional[Callable[[Optional[str]], int]] = None,
) -> dict:
    """Execute an update, migrating truth before target-format writers restart.

    Install/smoke failures still roll back. Once migration begins, the target
    checkout is retained on failure: the truth may already have crossed the
    one-way format boundary and rolling its reader back would be unsafe.
    ``retire`` disables unpinned fix-import override patches built
    against the older core (see :mod:`._repair.retire`) — after smoke so it
    only ever runs on a proven install, before restart so the reloading agents
    come back without stale overrides. Returns the result dict that also lands
    in ``health.json``."""
    assert plan.action == "update" and plan.tag
    reinstall = _default_reinstall if reinstall is None else reinstall
    smoke = _default_smoke if smoke is None else smoke
    restart = _default_restart if restart is None else restart
    retire = _default_retire if retire is None else retire
    migrate = _default_migrate if migrate is None else migrate
    home_format_version = (
        _default_home_format_version if home_format_version is None else home_format_version
    )
    needs_migration = (
        plan.target_format is not None
        and home_format_version(home) < plan.target_format
    )

    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    r = _git(repo, "checkout", "--quiet", plan.tag)
    if r.returncode != 0:
        return {"ok": False, "action": "failed", "current": plan.current, "tag": plan.tag,
                "reason": f"checkout {plan.tag} failed: {r.stderr.strip()}"}

    try:
        reinstall(repo)
        smoke(home)
    except Exception as e:  # noqa: BLE001 — anything here means roll back
        reason = str(e)
        logger.error("self-update: install of %s failed — rolling back to %s: %s",
                     plan.tag, prev[:12], reason)
        rb = _git(repo, "checkout", "--quiet", prev)
        rolled_back = rb.returncode == 0
        if rolled_back:
            try:
                reinstall(repo)
            except Exception as e2:  # noqa: BLE001 — report, nothing left to try
                rolled_back = False
                reason += f"; rollback reinstall also failed: {e2}"
        else:
            reason += f"; rollback checkout also failed: {rb.stderr.strip()}"
        return {"ok": False, "action": "rolled-back" if rolled_back else "failed",
                "current": plan.current, "tag": plan.tag, "reason": reason}

    if needs_migration:
        try:
            migrate(home)
            smoke(home)
        except Exception as e:  # noqa: BLE001 — never roll old code onto migrated truth
            reason = str(e)
            logger.error(
                "self-update: truth migration for %s failed; retaining target code: %s",
                plan.tag, reason,
            )
            return {
                "ok": False, "action": "migration-failed", "current": plan.current,
                "tag": plan.tag, "reason": reason, "rolled_back": False,
            }

    try:
        retire(home, plan.tag)
    except Exception as e:  # noqa: BLE001 — advisory; a good update must not roll back on this
        logger.warning("self-update: patch retirement after %s: %s", plan.tag, e)
    try:
        restart()
    except Exception as e:  # noqa: BLE001 — advisory; the update itself succeeded
        logger.warning("self-update: agent restart after %s: %s", plan.tag, e)
    return {
        "ok": True, "action": "updated", "current": plan.current, "tag": plan.tag,
        "reason": f"updated {plan.current} → {plan.tag}", "migrated": needs_migration,
    }


# ── orchestration ────────────────────────────────────────────────────────────


def self_update(
    home: Optional[str] = None,
    *,
    check_only: bool = False,
    allow_format_bump: bool = False,
    reinstall: Optional[Callable[[Path], None]] = None,
    smoke: Optional[Callable[[Optional[str]], None]] = None,
    restart: Optional[Callable[[], None]] = None,
    retire: Optional[Callable[[Optional[str], str], None]] = None,
) -> dict:
    """One full check-and-maybe-apply, recorded in ``health.json`` (the record
    behind the ``thread_archive status`` line and the viewer's health panel).
    ``check_only`` plans and reports without changing the installed
    checkout."""
    repo = install_repo()
    if repo is None:
        return {"ok": False, "action": "unavailable", "current": "",
                "reason": "not a git install — nothing to update against"}

    cfg = update_config(home)
    plan = plan_update(
        repo,
        remote=str(cfg.get("remote", DEFAULT_REMOTE)),
        allow_format_bump=allow_format_bump,
    )
    if plan.action == "update" and not check_only:
        result = apply_update(repo, plan, home=home,
                              reinstall=reinstall, smoke=smoke, restart=restart,
                              retire=retire)
    else:
        result = {"ok": plan.action != "blocked", "action": plan.action,
                  "current": plan.current, "tag": plan.tag, "reason": plan.reason}

    try:
        from ._ops.health import record_health

        record_health(HEALTH_KEY, result)
    except Exception:  # noqa: BLE001 — advisory
        logger.exception("self-update: could not record outcome in health.json")
    return result
