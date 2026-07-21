"""Service management for the archive daemons — the platform-neutral front.

The archive runs three long-lived agents (see :mod:`.spec`): the always-on
watcher, the shared MCP server, and the scheduled nightly backup. This package
schedules them through whichever backend fits the host — launchd on macOS
(:mod:`.launchd`), systemd on Linux (:mod:`.systemd`) — behind one interface.

Callers (``cli``, ``_setup.machine``, ``_update``) import only this module; they
never reach a concrete backend. Adding a platform is a drop-in backend that
``register()``s itself in :mod:`.base` — no edits here or in the callers.

Every convenience op resolves :func:`~.base.active_backend` live, so a process
whose platform is forced (the test seam) gets the matching backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Importing the backend modules registers them.
from . import launchd as _launchd_backend  # noqa: F401
from . import systemd as _systemd_backend  # noqa: F401
from .base import NullBackend, ServiceBackend, active_backend, registered_backends
from .spec import (
    MCP_DEFAULT_HOST,
    MCP_DEFAULT_PORT,
    backup_spec,
    entry_path,
    mcp_spec,
    watcher_spec,
)

__all__ = [
    "MCP_DEFAULT_HOST",
    "MCP_DEFAULT_PORT",
    "ServiceBackend",
    "NullBackend",
    "active_backend",
    "registered_backends",
    "can_schedule",
    "service_kind",
    "label",
    "entry_path",
    "install_watcher",
    "uninstall_watcher",
    "restart_watcher",
    "watcher_status",
    "install_mcp",
    "uninstall_mcp",
    "restart_mcp",
    "mcp_status",
    "install_backup",
    "uninstall_backup",
    "restart_backup",
    "backup_status",
    "agent_installed",
    "agent_loaded",
    "agent_home",
    "restart_agent",
    "backup_agent_dest",
]


def can_schedule(platform: Optional[str] = None) -> bool:
    """Whether ``platform`` (default: this host) has a service manager that can
    schedule the agents."""
    return active_backend(platform).name != "none"


def service_kind(platform: Optional[str] = None) -> Optional[str]:
    """The active backend's identity ("launchd"/"systemd"), or ``None`` when
    nothing on ``platform`` schedules — the marker the setup flow records."""
    backend = active_backend(platform)
    return None if backend.name == "none" else backend.name


def label(agent: str) -> str:
    """The active backend's platform id for a logical agent name."""
    return active_backend().label(agent)


def _log_dir(home: Optional[str]) -> Path:
    from .._config import resolve_paths

    return resolve_paths(home).home / "logs"


def install_watcher(
    home: Optional[str] = None, *, web: bool = True, web_port: int = 8787
) -> Path:
    entry = entry_path("thread_archive")
    spec = watcher_spec(entry, _log_dir(home), home=home, web=web, web_port=web_port)
    return active_backend().install(spec)


def uninstall_watcher() -> None:
    active_backend().uninstall("watcher")


def restart_watcher() -> None:
    active_backend().restart("watcher")


def watcher_status() -> str:
    return active_backend().status("watcher")


def install_mcp(
    home: Optional[str] = None,
    *,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
    ingest: bool = False,
) -> Path:
    entry = entry_path("archive-mcp")
    spec = mcp_spec(entry, _log_dir(home), home=home, host=host, port=port, ingest=ingest)
    return active_backend().install(spec)


def uninstall_mcp() -> None:
    active_backend().uninstall("mcp")


def restart_mcp() -> None:
    active_backend().restart("mcp")


def mcp_status() -> str:
    return active_backend().status("mcp")


def install_backup(
    dest: str,
    home: Optional[str] = None,
    *,
    hour: int = 4,
    minute: int = 0,
    notify_url: Optional[str] = None,
) -> Path:
    entry = entry_path("thread_archive")
    spec = backup_spec(
        entry, _log_dir(home), dest, home=home, hour=hour, minute=minute, notify_url=notify_url
    )
    return active_backend().install(spec)


def uninstall_backup() -> None:
    active_backend().uninstall("backup")


def restart_backup() -> None:
    active_backend().restart("backup")


def backup_status() -> str:
    return active_backend().status("backup")


def agent_installed(agent: str) -> bool:
    return active_backend().is_installed(agent)


def agent_loaded(agent: str) -> bool:
    return active_backend().is_loaded(agent)


def agent_home(agent: str) -> Optional[str]:
    return active_backend().agent_home(agent)


def restart_agent(agent: str) -> None:
    active_backend().restart(agent)


def backup_agent_dest() -> Optional[str]:
    return active_backend().backup_dest()
