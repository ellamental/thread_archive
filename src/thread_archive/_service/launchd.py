"""The macOS (launchd) service backend.

Renders an :class:`~.spec.AgentSpec` into a LaunchAgent plist and drives the
agent's lifecycle through ``launchctl`` under ``gui/$(id -u)``. ``thread-archive service
install`` materializes a plist from inside the package — pointing at the
installed console script, wherever this Python environment put it — and loads it.
No repo checkout, no Makefile, no sed: this is the "upgrade to always-fresh" step
after a plain ``pip install thread-archive``.

Each plist mirrors what its agent needs and nothing else: run at login in the
Aqua session (the archive home and watched stores live in the user's home), logs
under ``<archive home>/logs``. The pure ``*_plist`` builders are unit-tested in
``tests/test_launchd.py``; the install/uninstall/restart/status wrappers are thin
``launchctl`` shells around them.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .base import register
from .spec import (
    BACKUP_DEFAULT_HOUR,
    BACKUP_DEFAULT_MINUTE,
    MCP_DEFAULT_HOST,
    MCP_DEFAULT_PORT,
    RESTART_SEC,
    AgentSpec,
    Restart,
    backup_spec,
    entry_path,
    mcp_spec,
    watcher_spec,
)

# Kept importable here (not only from .spec) so callers and tests that reach the
# launchd backend directly see the same console-script finder.
_entry_path = entry_path

WATCHER_LABEL = "com.thread-archive.watcher"
MCP_LABEL = "com.thread-archive.mcp"
BACKUP_LABEL = "com.thread-archive.backup"

_LABELS = {"watcher": WATCHER_LABEL, "mcp": MCP_LABEL, "backup": BACKUP_LABEL}


def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise SystemExit("thread-archive service: launchd management is macOS-only")


def _launchd_path(bin_dir: Path) -> str:
    # The entry's own bin dir first so `archive` resolves its interpreter; then
    # the usual macOS locations (launchd inherits no shell PATH).
    return f"{bin_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _plist(spec: AgentSpec) -> dict:
    """An :class:`AgentSpec` as a launchd plist dict (pure — no filesystem, no
    launchctl)."""
    d: dict = {"Label": _LABELS[spec.name], "ProgramArguments": list(spec.argv)}
    if spec.schedule is not None:
        # A scheduled one-shot: fire daily, don't RunAtLoad. launchd runs a missed
        # fire on the next wake, so a run landing mid-morning (box asleep at the
        # mark) is normal, not a fault.
        d["StartCalendarInterval"] = {
            "Hour": spec.schedule.hour,
            "Minute": spec.schedule.minute,
        }
    d["RunAtLoad"] = spec.run_at_load
    # The user's GUI login session — that's where the watched home-dir stores
    # (~/.claude, ~/.codex, Cursor, …) live.
    d["LimitLoadToSessionType"] = "Aqua"
    if spec.restart is Restart.ALWAYS:
        d["KeepAlive"] = True
    elif spec.restart is Restart.ON_FAILURE:
        d["KeepAlive"] = {"Crashed": True}
    # Restart.NEVER → no KeepAlive (a run-to-completion scheduled job).
    if spec.restart is not Restart.NEVER:
        d["ThrottleInterval"] = RESTART_SEC
    # App Nap must not stall a live indexer or an interactive server; the nice'd,
    # I/O-bound backup runs Background.
    d["ProcessType"] = "Background" if (spec.nice is not None or spec.io_idle) else "Standard"
    d["SoftResourceLimits"] = {"NumberOfFiles": spec.nofile}
    d["WorkingDirectory"] = str(spec.working_dir)
    d["StandardOutPath"] = str(spec.log_stdout)
    d["StandardErrorPath"] = str(spec.log_stderr)
    env = {"PATH": _launchd_path(spec.bin_dir)}
    env.update(spec.env)
    d["EnvironmentVariables"] = env
    return d


# ── pure builders (kept for tests + any direct caller) ────────────────────────


def watcher_plist(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    web: bool = True,
    web_port: int = 8787,
    has_viewer: Optional[bool] = None,
) -> dict:
    """The watcher LaunchAgent as a plist dict (pure).

    ``has_viewer`` defaults to probing this installation — see
    :func:`.spec.watcher_spec`.
    """
    return _plist(watcher_spec(entry, log_dir, home=home, web=web, web_port=web_port,
                               has_viewer=has_viewer))


def mcp_plist(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
    ingest: bool = False,
) -> dict:
    """The shared-MCP-server LaunchAgent as a plist dict (pure)."""
    return _plist(mcp_spec(entry, log_dir, home=home, host=host, port=port, ingest=ingest))


def backup_plist(
    entry: Path,
    log_dir: Path,
    dest: str,
    *,
    home: Optional[str] = None,
    hour: int = BACKUP_DEFAULT_HOUR,
    minute: int = BACKUP_DEFAULT_MINUTE,
    notify_url: Optional[str] = None,
) -> dict:
    """The nightly-backup LaunchAgent as a plist dict (pure)."""
    return _plist(
        backup_spec(
            entry, log_dir, dest, home=home, hour=hour, minute=minute, notify_url=notify_url
        )
    )


# ── launchctl lifecycle ───────────────────────────────────────────────────────


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _uid() -> int:
    import os

    return os.getuid()


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=check, capture_output=True, text=True)


def _install_agent(label: str, plist_dict: dict, log_dir: Path) -> Path:
    """Write ``label``'s plist and (re)load the agent. Returns the plist path.

    Idempotent: an existing agent is booted out first, so re-running after an
    upgrade or a config change is the supported way to apply it.
    """
    _require_darwin()
    # launchd won't mkdir StandardOutPath's parent — a missing dir fails the
    # load silently.
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = _plist_path(label)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(plist_dict))
    domain = f"gui/{_uid()}"
    if _launchctl("bootout", f"{domain}/{label}").returncode == 0:
        # Let bootout settle before bootstrap (avoids 'Bootstrap failed: 5:
        # Input/output error').
        time.sleep(3)
    result = _launchctl("bootstrap", domain, str(plist))
    if result.returncode != 0:
        raise SystemExit(
            f"thread-archive service: launchctl bootstrap failed: {result.stderr.strip()}"
        )
    return plist


def _uninstall_agent(label: str) -> None:
    _require_darwin()
    _launchctl("bootout", f"gui/{_uid()}/{label}")
    _plist_path(label).unlink(missing_ok=True)


def _restart_agent(label: str) -> None:
    """Apply a code edit: kick the running agent (the plist itself is only
    re-read on install)."""
    _require_darwin()
    result = _launchctl("kickstart", "-k", f"gui/{_uid()}/{label}")
    if result.returncode != 0:
        raise SystemExit(
            f"thread-archive service: launchctl kickstart failed: {result.stderr.strip()}"
        )


def _agent_status(label: str) -> str:
    """A short human-readable status: state/pid/program, or 'not loaded'."""
    _require_darwin()
    result = _launchctl("print", f"gui/{_uid()}/{label}")
    if result.returncode != 0:
        return f"{label}: not loaded"
    lines = [
        ln.strip()
        for ln in result.stdout.splitlines()
        if any(k in ln for k in ("state =", "pid =", "program ="))
    ]
    return "\n".join([label, *lines])


def _agent_loaded(label: str) -> bool:
    _require_darwin()
    return _launchctl("print", f"gui/{_uid()}/{label}").returncode == 0


def _agent_home(label: str) -> Optional[str]:
    """The ``THREAD_ARCHIVE_HOME`` the installed agent's plist pins, or ``None``
    when the plist sets none (the default-home wiring) or can't be read. Reads
    the plist off disk, so it reflects the agent even when it isn't loaded."""
    try:
        plist = plistlib.loads(_plist_path(label).read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    return plist.get("EnvironmentVariables", {}).get("THREAD_ARCHIVE_HOME")


# ── the backend ───────────────────────────────────────────────────────────────


class LaunchdBackend:
    """The macOS service backend (see :class:`~.base.ServiceBackend`)."""

    name = "launchd"

    def available(self, platform: str) -> bool:
        return platform == "darwin"

    def label(self, agent: str) -> str:
        return _LABELS[agent]

    def render(self, spec: AgentSpec) -> dict:
        return _plist(spec)

    def install(self, spec: AgentSpec) -> Path:
        return _install_agent(_LABELS[spec.name], _plist(spec), spec.log_stdout.parent)

    def uninstall(self, agent: str) -> None:
        _uninstall_agent(_LABELS[agent])

    def restart(self, agent: str) -> None:
        _restart_agent(_LABELS[agent])

    def status(self, agent: str) -> str:
        return _agent_status(_LABELS[agent])

    def is_installed(self, agent: str) -> bool:
        return _plist_path(_LABELS[agent]).exists()

    def is_loaded(self, agent: str) -> bool:
        return _agent_loaded(_LABELS[agent])

    def agent_home(self, agent: str) -> Optional[str]:
        return _agent_home(_LABELS[agent])

    def backup_dest(self) -> Optional[str]:
        return backup_agent_dest()


_BACKEND = LaunchdBackend()


# ── module-level wrappers over the backend ────────────────────────────────────


def _log_dir(home: Optional[str]) -> Path:
    from .._config import resolve_paths

    return resolve_paths(home).home / "logs"


def install_watcher(
    home: Optional[str] = None, *, web: bool = True, web_port: int = 8787
) -> Path:
    entry = _entry_path("thread-archive")
    return _BACKEND.install(
        watcher_spec(entry, _log_dir(home), home=home, web=web, web_port=web_port)
    )


def uninstall_watcher() -> None:
    _BACKEND.uninstall("watcher")


def restart_watcher() -> None:
    _BACKEND.restart("watcher")


def watcher_status() -> str:
    return _BACKEND.status("watcher")


def install_mcp(
    home: Optional[str] = None,
    *,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
    ingest: bool = False,
) -> Path:
    entry = _entry_path("archive-mcp")
    return _BACKEND.install(
        mcp_spec(entry, _log_dir(home), home=home, host=host, port=port, ingest=ingest)
    )


def uninstall_mcp() -> None:
    _BACKEND.uninstall("mcp")


def restart_mcp() -> None:
    _BACKEND.restart("mcp")


def mcp_status() -> str:
    return _BACKEND.status("mcp")


def install_backup(
    dest: str,
    home: Optional[str] = None,
    *,
    hour: int = BACKUP_DEFAULT_HOUR,
    minute: int = BACKUP_DEFAULT_MINUTE,
    notify_url: Optional[str] = None,
) -> Path:
    entry = _entry_path("thread-archive")
    return _BACKEND.install(
        backup_spec(
            entry, _log_dir(home), dest, home=home, hour=hour, minute=minute,
            notify_url=notify_url,
        )
    )


def uninstall_backup() -> None:
    _BACKEND.uninstall("backup")


def restart_backup() -> None:
    _BACKEND.restart("backup")


def backup_status() -> str:
    return _BACKEND.status("backup")


def backup_agent_dest() -> Optional[str]:
    """The ``dest`` the installed backup agent runs against, or ``None`` when no
    plist is present or it isn't the package-generated shape (e.g. an external
    wrapper-script form, whose dest lives inside the wrapper's args). Reads the
    plist on disk, so it reflects the agent even when it isn't loaded."""
    if sys.platform != "darwin":
        return None
    try:
        plist = plistlib.loads(_plist_path(BACKUP_LABEL).read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    args = plist.get("ProgramArguments", [])
    try:
        return args[args.index("nightly") + 1]
    except (ValueError, IndexError):
        return None


register(_BACKEND)
