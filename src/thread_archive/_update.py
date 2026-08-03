"""Self-update from PyPI — how a release reaches a packaged install.

A packaged install is a wheel in some environment's site-packages, so its
update is a ``pip install`` of the newest release plus an agent restart. This
module automates exactly that, against whatever index this environment's pip
resolves (PyPI by default). A **source install** — the editable install a git
clone makes — has no package to upgrade: its code *is* the checkout, moving it
is git's job, and this module reports it as unavailable rather than touching
the clone.

**The operator drives it.** ``thread-archive self-update`` is the whole
mechanism: nothing polls for releases, nothing applies one on its own, and no
configuration turns unattended apply on. ``--check`` resolves and reports
without changing the environment. Guardrails on the explicit operation, on a
machine that holds someone's entire conversation history:

- **Released versions only.** The candidate is the newest ``X.Y.Z`` release pip
  resolves *for this interpreter* — a release that raised its Python floor is
  simply not offered here. Pre-releases are never taken (nothing passes
  ``--pre``), and a candidate no newer than the running ``__version__`` reads
  as up-to-date, never as a downgrade.
- **What is inspected is what is installed.** The candidate wheel is downloaded
  once (``pip download --no-deps``), gated on what that file declares, and then
  installed from the file itself — the gate cannot be read off one artifact and
  applied to another.
- **Never across a truth-format bump without asking.** A reader refuses a truth
  directory newer than it understands (docs/public/format.md), so applying a release
  that bumps ``TRUTH_FORMAT_VERSION`` makes rollback a hard stop the moment the
  new code touches the store. The candidate's declared format version is read
  out of the wheel; if it is newer than ours — or cannot be determined — the
  update requires ``thread-archive self-update --allow-format-bump``.
- **Verify, then roll back.** After the install, the new code must pass a smoke
  check (``thread-archive status`` under it). On failure the previous version
  is reinstalled from the index — the archive keeps running the code that
  worked.

The outcome of each run is stamped in ``health.json``, which is what
``thread-archive status`` and the web viewer's health panel report. Config
rides ``config.json``::

    {"update": {"enabled": true}}

The install keeps the shape it had: this upgrades the base distribution, and
dependencies that arrived with an extra stay installed at the versions they are
at — the same thing ``pip install -U thread-archive`` does by hand. An
environment whose packages someone else manages (a ``uv tool`` or ``pipx``
install, or any environment without pip) is reported as unavailable, naming
that manager's own upgrade command.

Trust anchor: HTTPS to the index the environment resolves against, plus the
build attestation PyPI records for what this project's release workflow
publishes. There is no separate signature layer here.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DIST = "thread-archive"

# Network ops (download, install) get the long leash; the local probes are fast
# but not instant on a cold disk. The smoke check and migration get their own.
_PIP_TIMEOUT = 900
_PROBE_TIMEOUT = 60
_SMOKE_TIMEOUT = 300
_MIGRATE_TIMEOUT = 1800

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_FORMAT_RE = re.compile(r"TRUTH_FORMAT_VERSION\s*=\s*(\d+)")

HEALTH_KEY = "self_update_last"


@dataclass
class UpdatePlan:
    """The decision one check reached — what to do, why, and off which file."""

    action: str  # "up-to-date" | "update" | "blocked"
    reason: str
    current: str
    target: Optional[str] = None
    wheel: Optional[Path] = None
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


# ── the install shape ────────────────────────────────────────────────────────


def source_checkout() -> Optional[Path]:
    """The git clone this package runs from, or ``None`` when it runs from a
    packaged install. A clone's code is the checkout — nothing pip installs can
    move it, so this is what makes self-update unavailable."""
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def managed_environment(prefix: Optional[Path] = None) -> Optional[str]:
    """The upgrade command for an environment whose packages a tool other than
    pip owns, or ``None`` when pip is the way in.

    ``uv tool`` and ``pipx`` each leave a receipt beside the environment they
    built. Driving pip inside one either fails outright (a ``uv tool`` venv has
    no pip) or leaves the manager's own record of what is installed lying about
    the version — so the manager's command is the answer, not this module's."""
    prefix = Path(sys.prefix) if prefix is None else prefix
    if (prefix / "uv-receipt.toml").is_file():
        return f"uv tool upgrade {DIST}"
    if (prefix / "pipx_metadata.json").is_file():
        return f"pipx upgrade {DIST}"
    return None


def _pip(*args: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def pip_available() -> bool:
    """Whether this interpreter can run pip at all — an environment built
    without it can be read, but never upgraded from in-process."""
    try:
        return _pip("--version", timeout=_PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── the candidate wheel ──────────────────────────────────────────────────────


def _parse_version(version: str) -> Optional[tuple[int, int, int]]:
    m = _VERSION_RE.match(version)
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


# Where TRUTH_FORMAT_VERSION lives inside the wheel. The anchored probe reads
# exactly this file so an unrelated assignment elsewhere in the artifact can
# never shadow the real constant on a safety gate.
_FORMAT_HOME = "thread_archive/_truth/layout.py"


def _wheel_format_version(wheel: Path) -> Optional[int]:
    """The ``TRUTH_FORMAT_VERSION`` a candidate wheel's code declares, read out
    of the wheel itself. Probes the constant's home module first; a scan of the
    artifact's other modules is the fallback so the check survives the module
    moving in a future release. ``None`` when it can't be found — callers treat
    that as a gate, not a pass."""
    try:
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            ordered = ([_FORMAT_HOME] if _FORMAT_HOME in names else []) + sorted(
                n for n in names if n.endswith(".py") and n != _FORMAT_HOME
            )
            for name in ordered:
                m = _FORMAT_RE.search(z.read(name).decode("utf-8", "replace"))
                if m is not None:
                    return int(m.group(1))
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        logger.warning("self-update: could not read %s: %s", wheel.name, e)
    return None


def download_candidate(
    dest: Path, *, pip_args: Sequence[str] = ()
) -> tuple[Optional[Path], str]:
    """Fetch the newest release wheel into ``dest``; return it and an empty
    reason, or ``None`` and why not.

    ``--no-deps`` keeps this to the one artifact under judgement, and
    ``--only-binary`` keeps it an inspectable wheel rather than a source tree
    that would have to be built to answer the format gate."""
    try:
        r = _pip(
            "download", "--no-deps", "--only-binary", ":all:", "--quiet",
            "--dest", str(dest), *pip_args, DIST, timeout=_PIP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"`pip download {DIST}` timed out"
    except OSError as e:
        return None, f"could not run pip: {e}"
    if r.returncode != 0:
        return None, f"`pip download {DIST}` failed: {(r.stderr or r.stdout).strip()[-300:]}"
    wheels = sorted(dest.glob("*.whl"))
    if not wheels:
        return None, f"pip resolved no wheel for {DIST}"
    return wheels[0], ""


def _wheel_version(wheel: Path) -> Optional[str]:
    """The version a wheel's filename declares (``name-version-…​.whl``)."""
    parts = wheel.name.split("-")
    return parts[1] if len(parts) >= 3 else None


# ── plan ─────────────────────────────────────────────────────────────────────


def plan_update(
    dest: Path,
    *,
    pip_args: Sequence[str] = (),
    allow_format_bump: bool = False,
    current_version: Optional[str] = None,
    local_format_version: Optional[int] = None,
) -> UpdatePlan:
    """Decide whether — and to which release — this install should update.

    Nothing here changes the environment: the candidate lands in ``dest``, a
    caller-owned scratch dir, and is read there. A format-gated candidate blocks
    rather than falling back to a lower release — the operator asked for the
    newest one, and stopping short of it deserves to be said out loud.
    """
    version: str = _running_version() if current_version is None else current_version
    local_fmt: int = (
        _local_format_version() if local_format_version is None else local_format_version
    )
    current = _parse_version(version)
    if current is None:
        return UpdatePlan("blocked", f"unparseable running version {version!r}", version)

    wheel, why = download_candidate(dest, pip_args=pip_args)
    if wheel is None:
        return UpdatePlan("blocked", why, version)

    target = _wheel_version(wheel)
    parsed = _parse_version(target or "")
    if parsed is None:
        return UpdatePlan(
            "blocked", f"{wheel.name} does not name a plain X.Y.Z release", version,
        )
    if parsed <= current:
        return UpdatePlan(
            "up-to-date", f"{version} is the newest release for this install", version,
            target=target,
        )

    fmt = _wheel_format_version(wheel)
    if fmt is None:
        return UpdatePlan(
            "blocked", f"{target} does not declare a readable truth-format version",
            version, target=target, wheel=wheel, current_format=local_fmt,
        )
    if not allow_format_bump and fmt > local_fmt:
        return UpdatePlan(
            "blocked",
            f"{target} declares truth-format version {fmt} > local {local_fmt} — "
            "a one-way door; run `thread-archive self-update --allow-format-bump` deliberately",
            version, target=target, wheel=wheel,
            current_format=local_fmt, target_format=fmt,
        )
    return UpdatePlan(
        "update", f"{target} is the newest release", version,
        target=target, wheel=wheel, current_format=local_fmt, target_format=fmt,
    )


# ── apply ────────────────────────────────────────────────────────────────────


def _default_install(requirement: str, *, pip_args: Sequence[str] = ()) -> None:
    r = _pip("install", "--quiet", *pip_args, requirement, timeout=_PIP_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"pip install failed: {(r.stderr or r.stdout).strip()[-500:]}")


def _default_smoke(home: Optional[str]) -> None:
    """The new install must stand up and read the archive: the console script
    exists, the package imports, the store opens. `thread-archive status` is exactly
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
            f"`thread-archive status` under the new install failed: {r.stderr.strip()[-500:]}"
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


def _default_retire(home: Optional[str], target: str) -> None:
    """Disable unpinned ``thread-archive source fix`` override patches built against a
    core older than ``target`` — patches are temporary bridges to the next release
    by default, and pinned ones opt out (see :mod:`._repair.retire`)."""
    from ._repair import retire_patches

    retire_patches(home, target=target)


def apply_update(
    plan: UpdatePlan,
    *,
    home: Optional[str] = None,
    install: Optional[Callable[[str], None]] = None,
    smoke: Optional[Callable[[Optional[str]], None]] = None,
    restart: Optional[Callable[[], None]] = None,
    retire: Optional[Callable[[Optional[str], str], None]] = None,
    migrate: Optional[Callable[[Optional[str]], None]] = None,
    home_format_version: Optional[Callable[[Optional[str]], int]] = None,
) -> dict:
    """Execute an update, migrating truth before target-format writers restart.

    The forward install is the inspected wheel by path; a rollback is the
    running version pinned by name, which the index still serves. Install/smoke
    failures roll back. Once migration begins, the target install is retained on
    failure: the truth may already have crossed the one-way format boundary and
    rolling its reader back would be unsafe. ``retire`` disables unpinned
    fix-import override patches built against the older core (see
    :mod:`._repair.retire`) — after smoke so it only ever runs on a proven
    install, before restart so the reloading agents come back without stale
    overrides. Returns the result dict that also lands in ``health.json``."""
    assert plan.action == "update" and plan.target and plan.wheel
    install = _default_install if install is None else install
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

    try:
        install(str(plan.wheel))
        smoke(home)
    except Exception as e:  # noqa: BLE001 — anything here means roll back
        reason = str(e)
        logger.error("self-update: install of %s failed — rolling back to %s: %s",
                     plan.target, plan.current, reason)
        rolled_back = True
        try:
            install(f"{DIST}=={plan.current}")
        except Exception as e2:  # noqa: BLE001 — report, nothing left to try
            rolled_back = False
            reason += f"; rollback to {plan.current} also failed: {e2}"
        return {"ok": False, "action": "rolled-back" if rolled_back else "failed",
                "current": plan.current, "target": plan.target, "reason": reason}

    if needs_migration:
        try:
            migrate(home)
            smoke(home)
        except Exception as e:  # noqa: BLE001 — never roll old code onto migrated truth
            reason = str(e)
            logger.error(
                "self-update: truth migration for %s failed; retaining target code: %s",
                plan.target, reason,
            )
            return {
                "ok": False, "action": "migration-failed", "current": plan.current,
                "target": plan.target, "reason": reason, "rolled_back": False,
            }

    try:
        retire(home, plan.target)
    except Exception as e:  # noqa: BLE001 — advisory; a good update must not roll back on this
        logger.warning("self-update: patch retirement after %s: %s", plan.target, e)
    try:
        restart()
    except Exception as e:  # noqa: BLE001 — advisory; the update itself succeeded
        logger.warning("self-update: agent restart after %s: %s", plan.target, e)
    return {
        "ok": True, "action": "updated", "current": plan.current, "target": plan.target,
        "reason": f"updated {plan.current} → {plan.target}", "migrated": needs_migration,
    }


# ── orchestration ────────────────────────────────────────────────────────────


def self_update(
    home: Optional[str] = None,
    *,
    check_only: bool = False,
    allow_format_bump: bool = False,
    pip_args: Sequence[str] = (),
    install: Optional[Callable[[str], None]] = None,
    smoke: Optional[Callable[[Optional[str]], None]] = None,
    restart: Optional[Callable[[], None]] = None,
    retire: Optional[Callable[[Optional[str], str], None]] = None,
) -> dict:
    """One full check-and-maybe-apply, recorded in ``health.json`` (the record
    behind the ``thread-archive status`` line and the viewer's health panel).
    ``check_only`` resolves and reports without installing anything.
    ``pip_args`` rides every pip invocation — what lets the suite resolve
    against a local index instead of the live one."""
    version = _running_version()
    checkout = source_checkout()
    manager = managed_environment()
    if checkout is not None:
        result = {
            "ok": False, "action": "unavailable", "current": version,
            "reason": f"source install — this runs from the checkout at {checkout}, "
                      "which is not an install shape this moves; its code is the "
                      "checkout, and git is what moves it",
        }
    elif manager is not None:
        result = {"ok": False, "action": "unavailable", "current": version,
                  "reason": f"this environment is managed — update with `{manager}`"}
    elif not pip_available():
        result = {"ok": False, "action": "unavailable", "current": version,
                  "reason": f"no pip in this environment ({sys.executable}) — "
                            f"update {DIST} with whatever installed it"}
    else:
        with tempfile.TemporaryDirectory(prefix="thread-archive-update-") as scratch:
            plan = plan_update(
                Path(scratch), pip_args=pip_args,
                allow_format_bump=allow_format_bump, current_version=version,
            )
            if plan.action == "update" and not check_only:
                result = apply_update(
                    plan, home=home,
                    install=install or (lambda req: _default_install(req, pip_args=pip_args)),
                    smoke=smoke, restart=restart, retire=retire,
                )
            else:
                result = {"ok": plan.action != "blocked", "action": plan.action,
                          "current": plan.current, "target": plan.target,
                          "reason": plan.reason}

    try:
        from ._ops.health import record_health

        record_health(HEALTH_KEY, result)
    except Exception:  # noqa: BLE001 — advisory
        logger.exception("self-update: could not record outcome in health.json")
    return result
