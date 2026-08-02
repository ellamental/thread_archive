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

from collections.abc import Callable
from pathlib import Path
from typing import Optional

# Importing the backend modules registers them.
from . import launchd as _launchd_backend  # noqa: F401
from . import systemd as _systemd_backend  # noqa: F401
from .base import NullBackend, ServiceBackend, active_backend, registered_backends
from .spec import (
    MCP_DEFAULT_HOST,
    MCP_DEFAULT_PORT,
    AgentSpec,
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
    "install_agent",
    "agent_status",
    "agent_installed",
    "agent_loaded",
    "agent_home",
    "restart_agent",
    "uninstall_agent",
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


#: How each agent's manifest is built: the console script it runs, and the spec
#: builder that shapes it. Installation is the only op where the three differ —
#: every other verb takes the agent's name and nothing else — so this table plus
#: a builder in :mod:`.spec` is the whole of adding one.
_SPEC_BUILDERS: dict[str, tuple[str, Callable[..., AgentSpec]]] = {
    "watcher": ("thread-archive", watcher_spec),
    "mcp": ("archive-mcp", mcp_spec),
    "backup": ("thread-archive", backup_spec),
}


def install_agent(agent: str, home: Optional[str] = None, **options) -> Path:
    """Schedule one agent by logical name; returns the manifest the backend wrote.

    ``options`` are that agent's own spec knobs (``web``/``web_port`` for the
    watcher, ``host``/``port``/``ingest`` for MCP, ``dest``/``hour``/``minute``/
    ``notify_url`` for the backup) — see :mod:`.spec` for each builder's defaults."""
    entry_name, build = _SPEC_BUILDERS[agent]
    return active_backend().install(
        build(entry_path(entry_name), _log_dir(home), home=home, **options)
    )


def agent_status(agent: str) -> str:
    return active_backend().status(agent)


def agent_installed(agent: str) -> bool:
    return active_backend().is_installed(agent)


def agent_loaded(agent: str) -> bool:
    return active_backend().is_loaded(agent)


def agent_home(agent: str) -> Optional[str]:
    return active_backend().agent_home(agent)


def restart_agent(agent: str) -> None:
    active_backend().restart(agent)


def uninstall_agent(agent: str) -> None:
    active_backend().uninstall(agent)


def backup_agent_dest() -> Optional[str]:
    return active_backend().backup_dest()
