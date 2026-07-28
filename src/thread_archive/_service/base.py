"""The service-backend contract and registry.

A :class:`ServiceBackend` is one platform's way of scheduling the archive's
agents (:mod:`.launchd` on macOS, :mod:`.systemd` on Linux). Backends
:func:`register` themselves; :func:`active_backend` resolves the one that fits
this host at call time (so a test that forces ``sys.platform`` gets the matching
backend), falling back to :class:`NullBackend` where nothing schedules.

Adding a platform is a drop-in: implement this Protocol, ``register()`` an
instance, done — no edits to the CLI, setup flow, or self-updater, which all go
through :mod:`.` (the package front), never a concrete backend.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from .spec import AgentSpec

# Logical agent names every backend must map to its own platform id.
AGENT_NAMES = ("watcher", "mcp", "backup")


@runtime_checkable
class ServiceBackend(Protocol):
    """One platform's scheduler for the archive agents.

    ``name`` is the backend's own identity ("launchd"/"systemd") — the marker the
    setup flow records. ``available()`` decides whether this backend can run on
    the current host. The rest operate on a **logical** agent name
    (``"watcher"``/``"mcp"``/``"backup"``); ``label`` maps that to the platform's
    own id (a launchd label, a systemd unit).
    """

    name: str

    def available(self, platform: str) -> bool: ...

    def label(self, agent: str) -> str: ...

    # Manifest rendering — pure, no filesystem or subprocess (for tests).
    def render(self, spec: AgentSpec) -> object: ...

    # Lifecycle.
    def install(self, spec: AgentSpec) -> Path: ...

    def uninstall(self, agent: str) -> None: ...

    def restart(self, agent: str) -> None: ...

    def status(self, agent: str) -> str: ...

    # Probes.
    def is_installed(self, agent: str) -> bool: ...

    def is_loaded(self, agent: str) -> bool: ...

    def agent_home(self, agent: str) -> Optional[str]: ...

    def backup_dest(self) -> Optional[str]: ...


class NullBackend:
    """The backend for a host with no supported scheduler.

    ``available()`` is always false, so :func:`active_backend` only ever returns
    it as the fallback; the lifecycle verbs refuse with a clear message rather
    than pretending to schedule anything.
    """

    name = "none"

    def available(self, platform: str) -> bool:
        return False

    def label(self, agent: str) -> str:
        return agent

    def render(self, spec: AgentSpec) -> object:
        raise SystemExit("thread-archive service: no service manager on this platform")

    def install(self, spec: AgentSpec) -> Path:
        raise SystemExit("thread-archive service: no service manager on this platform")

    def uninstall(self, agent: str) -> None:
        raise SystemExit("thread-archive service: no service manager on this platform")

    def restart(self, agent: str) -> None:
        raise SystemExit("thread-archive service: no service manager on this platform")

    def status(self, agent: str) -> str:
        return f"{agent}: no service manager on this platform"

    def is_installed(self, agent: str) -> bool:
        return False

    def is_loaded(self, agent: str) -> bool:
        return False

    def agent_home(self, agent: str) -> Optional[str]:
        return None

    def backup_dest(self) -> Optional[str]:
        return None


_BACKENDS: list[ServiceBackend] = []
_NULL = NullBackend()


def register(backend: ServiceBackend) -> None:
    """Add a backend to the registry (idempotent by ``name``)."""
    if any(b.name == backend.name for b in _BACKENDS):
        return
    _BACKENDS.append(backend)


def registered_backends() -> list[ServiceBackend]:
    """Every registered backend, in registration order (for conformance tests)."""
    return list(_BACKENDS)


def active_backend(platform: Optional[str] = None) -> ServiceBackend:
    """The backend that fits ``platform`` (default: this host's ``sys.platform``),
    else :class:`NullBackend`.

    ``platform`` is an explicit seam: pass it to resolve for a host other than
    this one — a test naming the platform it wants, rather than reaching over
    ``sys``. Resolved live (not cached).
    """
    plat = platform if platform is not None else sys.platform
    for backend in _BACKENDS:
        if backend.available(plat):
            return backend
    return _NULL
