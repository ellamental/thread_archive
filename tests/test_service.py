"""The service-backend plugin contract (``_service`` front + ``_service.base``).

The safety net for a maintainer adding a platform: the conformance test runs over
*every* registered backend, so a future Windows backend has to clear the same bar
the launchd and systemd backends do. Plus the resolver (which backend fits this
host) and the neutral :class:`AgentSpec` builders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thread_archive import _service
from thread_archive._service.base import (
    NullBackend,
    ServiceBackend,
    active_backend,
    registered_backends,
)
from thread_archive._service.spec import (
    DailyAt,
    Restart,
    backup_spec,
    mcp_spec,
    watcher_spec,
)

ENTRY = Path("/opt/venv/bin/thread_archive")
LOG_DIR = Path("/data/arc/logs")
AGENTS = ("watcher", "mcp", "backup")


def _specs():
    return [
        watcher_spec(ENTRY, LOG_DIR, home="/data/arc"),
        mcp_spec(ENTRY, LOG_DIR, home="/data/arc"),
        backup_spec(ENTRY, LOG_DIR, "/vol/bak", home="/data/arc"),
    ]


# ── the conformance bar (every registered backend, incl. any future one) ──────


@pytest.mark.parametrize("backend", registered_backends(), ids=lambda b: b.name)
def test_backend_satisfies_the_contract(backend) -> None:
    # The runtime-checkable Protocol: a backend missing a method fails here.
    assert isinstance(backend, ServiceBackend)
    assert isinstance(backend.name, str) and backend.name

    # It maps every logical agent to a non-empty platform id …
    labels = [backend.label(a) for a in AGENTS]
    assert all(isinstance(x, str) and x for x in labels)
    assert len(set(labels)) == len(AGENTS)  # distinct per agent

    # … and renders every spec to a non-empty manifest, purely (no filesystem).
    for spec in _specs():
        manifest = backend.render(spec)
        assert manifest


def test_both_platform_backends_are_registered() -> None:
    assert {b.name for b in registered_backends()} == {"launchd", "systemd"}


# ── the resolver ──────────────────────────────────────────────────────────────


# The platform is an explicit seam (`active_backend(platform=...)`), so these
# name the host they want rather than reaching over `sys`.


def test_active_backend_darwin() -> None:
    assert active_backend("darwin").name == "launchd"
    assert _service.can_schedule("darwin") is True
    assert _service.service_kind("darwin") == "launchd"


def test_active_backend_linux_with_systemctl(tmp_path, monkeypatch) -> None:
    # systemd.available() gates on a real systemctl on PATH (setenv, not a patch).
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", str(stub))
    assert active_backend("linux").name == "systemd"
    assert _service.service_kind("linux") == "systemd"


def test_active_backend_linux_without_systemctl(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")  # no systemctl reachable
    assert isinstance(active_backend("linux"), NullBackend)
    assert _service.can_schedule("linux") is False
    assert _service.service_kind("linux") is None


def test_active_backend_unsupported_platform() -> None:
    assert isinstance(active_backend("sunos5"), NullBackend)
    assert _service.can_schedule("sunos5") is False


def test_null_backend_refuses_to_schedule() -> None:
    null = NullBackend()
    assert null.available("linux") is False
    with pytest.raises(SystemExit, match="no service manager"):
        null.install(watcher_spec(ENTRY, LOG_DIR))
    assert null.is_installed("watcher") is False
    assert null.backup_dest() is None


# ── the neutral AgentSpec builders ────────────────────────────────────────────


def test_watcher_spec_semantics() -> None:
    spec = watcher_spec(ENTRY, LOG_DIR, home="/data/arc")
    assert spec.name == "watcher"
    assert spec.restart is Restart.ON_FAILURE
    assert spec.keep_trying is True
    assert spec.run_at_load is True
    assert spec.schedule is None
    assert spec.nice is None and spec.io_idle is False
    assert spec.env == {"THREAD_ARCHIVE_HOME": "/data/arc"}
    assert spec.argv[:2] == [str(ENTRY), "watch"]


def test_mcp_spec_semantics() -> None:
    spec = mcp_spec(ENTRY, LOG_DIR, ingest=True)
    assert spec.name == "mcp"
    assert spec.restart is Restart.ALWAYS  # many clients depend on it
    assert spec.run_at_load is True
    assert spec.env["THREAD_ARCHIVE_MCP_INGEST"] == "1"


def test_backup_spec_semantics() -> None:
    spec = backup_spec(ENTRY, LOG_DIR, "/vol/bak", hour=2, minute=30)
    assert spec.name == "backup"
    assert spec.restart is Restart.NEVER  # a run-to-completion scheduled job
    assert spec.run_at_load is False
    assert spec.schedule == DailyAt(2, 30)
    assert spec.nice == 10 and spec.io_idle is True
