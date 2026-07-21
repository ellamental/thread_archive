"""Real systemd ``--user`` lifecycle — the net-new platform surface, exercised
against an actual user service manager.

Gated behind ``THREAD_ARCHIVE_SYSTEMD_IT=1`` (set in the CI ``systemd`` job) so it
never runs — or clobbers a developer's real setup — unless explicitly asked for.
It needs a live user systemd, which the slim install container and the macOS box
don't have; the CI job brings one up (``loginctl enable-linger`` +
``XDG_RUNTIME_DIR``) and runs ``pytest -m systemd``.

It installs the real units under the login user's own ``~/.config/systemd/user``
so ``systemctl --user`` and the installer agree on the location, and always
uninstalls in a ``finally``. The installer resolves that path from ``$HOME``,
but conftest sandboxes ``$HOME`` for store isolation — which would write the
units somewhere the live user manager never scans. The ``_login_home`` fixture
restores the passwd home (the one the manager itself runs under) for this lane.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import time

import pytest

pytestmark = pytest.mark.systemd

_ENABLED = os.environ.get("THREAD_ARCHIVE_SYSTEMD_IT") == "1"
_SKIP = pytest.mark.skipif(
    not _ENABLED, reason="set THREAD_ARCHIVE_SYSTEMD_IT=1 (the CI systemd job) to run"
)


@pytest.fixture(autouse=True)
def _login_home(monkeypatch):
    """Run against the real login home, not conftest's sandbox.

    The installer writes units under ``Path.home()/.config/systemd/user`` and the
    live ``systemctl --user`` manager reads them from the login user's home (from
    the passwd database). conftest's autouse ``_isolate_home`` re-pins ``$HOME`` to
    a throwaway dir per test, which would send the units where the manager never
    looks. Pinning ``$HOME`` to the passwd home makes installer and manager agree;
    the archive's own store still goes to the test's ``--home`` tmp path."""
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)

WATCHER = "thread-archive-watcher.service"
BACKUP_TIMER = "thread-archive-backup.timer"


def _sysenv() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, env=_sysenv()
    )


def _is_active(unit: str) -> str:
    return _sc("is-active", unit).stdout.strip()


def _wait_active(unit: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_active(unit) == "active":
            return True
        time.sleep(0.5)
    return False


@_SKIP
def test_watcher_and_backup_lifecycle_on_real_systemd(tmp_path) -> None:
    from thread_archive import cli

    home = str(tmp_path / "arc")
    try:
        # install the watcher → it must come up active with a real PID. --no-web
        # keeps the lifecycle check off the viewer's port.
        assert cli.main(["daemon", "install", "--no-web", "--home", home]) == 0
        assert _wait_active(WATCHER), f"{WATCHER} not active: {_sc('status', WATCHER).stdout}"
        pid = _sc("show", "-p", "MainPID", WATCHER).stdout.strip()
        assert pid and pid != "MainPID=0"
        # the watcher process logs where the rest of the system reads.
        assert (tmp_path / "arc" / "logs").is_dir()

        # install the backup → the TIMER is armed (the service stays inactive
        # between fires).
        dest = str(tmp_path / "bak")
        assert cli.main(["daemon", "install", "--backup", "--dest", dest, "--home", home]) == 0
        assert _is_active(BACKUP_TIMER) == "active"
        assert BACKUP_TIMER in _sc("list-timers", "--all").stdout

        # restart applies a code edit without tearing anything down.
        assert cli.main(["daemon", "restart", "--no-web", "--home", home]) == 0
        assert _wait_active(WATCHER)
    finally:
        cli.main(["daemon", "uninstall", "--home", home])
        cli.main(["daemon", "uninstall", "--backup", "--home", home])

    # uninstall really removed them.
    assert _is_active(WATCHER) != "active"
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    assert not os.path.exists(os.path.join(unit_dir, WATCHER))
    assert not os.path.exists(os.path.join(unit_dir, BACKUP_TIMER))
