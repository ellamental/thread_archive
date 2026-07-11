"""LaunchAgent management for the always-on watcher — private machinery.

``archive daemon install`` materializes the watcher's LaunchAgent plist from
inside the package — pointing at the installed ``archive`` console script,
wherever this Python environment put it — and loads it. No repo checkout, no
Makefile, no sed: this is the "upgrade to always-fresh" step after a plain
``pip install thread-archive``, and it is what ``host/Makefile install-agent``
delegates to for the operator flow (which adds the thread-family manifest on
top; the manifest writer stays in ``host/``, repo-only by design).

macOS only, deliberately (launchd is the product's process manager). The
plist mirrors what the watcher needs and nothing else: run at login in the
Aqua session (the watched stores live in the user's home), restart on crash,
logs under ``<archive home>/logs``.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

WATCHER_LABEL = "com.thread-archive.watcher"


def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise SystemExit("archive daemon: launchd management is macOS-only")


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{WATCHER_LABEL}.plist"


def _entry_path() -> Path:
    """The ``archive`` console script this environment installed — the thing
    the plist must point at (launchd won't inherit a venv)."""
    candidate = Path(sys.executable).with_name("archive")
    if candidate.is_file():
        return candidate
    found = shutil.which("archive")
    if found:
        return Path(found)
    raise SystemExit(
        "archive daemon: cannot find the `archive` console script next to "
        f"{sys.executable} or on PATH — is the package installed in this environment?"
    )


def watcher_plist(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    web: bool = True,
    web_port: int = 8787,
) -> dict:
    """The watcher LaunchAgent as a plist dict (pure — no filesystem, no launchctl)."""
    args = [str(entry), "watch"]
    if web:
        # Cohost the read-only viewer in the watcher's own process so it has a
        # persistent URL — one process, one engine; WAL makes the concurrent
        # reads safe (see _store/_base.py).
        args += ["--web", "--web-port", str(web_port)]
    env = {
        # The entry's own bin dir first so `archive` resolves its interpreter.
        "PATH": f"{entry.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if home:
        env["THREAD_ARCHIVE_HOME"] = home
    return {
        "Label": WATCHER_LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        # The user's GUI login session — that's where the watched home-dir
        # stores (~/.claude, ~/.codex, Cursor, …) live.
        "LimitLoadToSessionType": "Aqua",
        "KeepAlive": {"Crashed": True},
        "ThrottleInterval": 5,
        # A live working-memory indexer: don't let App Nap stall the poll cadence.
        "ProcessType": "Standard",
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log_dir / "watcher-stdout.log"),
        "StandardErrorPath": str(log_dir / "watcher-stderr.log"),
        "EnvironmentVariables": env,
    }


def _uid() -> int:
    import os

    return os.getuid()


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], check=check, capture_output=True, text=True
    )


def install_watcher(
    home: Optional[str] = None, *, web: bool = True, web_port: int = 8787
) -> Path:
    """Write the plist and (re)load the agent. Returns the plist path.

    Idempotent: an existing agent is booted out first, so re-running after an
    upgrade or a config change is the supported way to apply it.
    """
    _require_darwin()
    from ._config import resolve_paths

    entry = _entry_path()
    log_dir = resolve_paths(home).home / "logs"
    # launchd won't mkdir StandardOutPath's parent — a missing dir fails the
    # load silently.
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(
        plistlib.dumps(watcher_plist(entry, log_dir, home=home, web=web, web_port=web_port))
    )
    domain = f"gui/{_uid()}"
    if _launchctl("bootout", f"{domain}/{WATCHER_LABEL}").returncode == 0:
        # Let bootout settle before bootstrap (avoids 'Bootstrap failed: 5:
        # Input/output error').
        time.sleep(3)
    result = _launchctl("bootstrap", domain, str(plist))
    if result.returncode != 0:
        raise SystemExit(
            f"archive daemon: launchctl bootstrap failed: {result.stderr.strip()}"
        )
    return plist


def uninstall_watcher() -> None:
    _require_darwin()
    _launchctl("bootout", f"gui/{_uid()}/{WATCHER_LABEL}")
    _plist_path().unlink(missing_ok=True)


def restart_watcher() -> None:
    """Apply a code edit: kick the running agent (the plist itself is only
    re-read on install)."""
    _require_darwin()
    result = _launchctl("kickstart", "-k", f"gui/{_uid()}/{WATCHER_LABEL}")
    if result.returncode != 0:
        raise SystemExit(
            f"archive daemon: launchctl kickstart failed: {result.stderr.strip()}"
        )


def watcher_status() -> str:
    """A short human-readable status: state/pid/program, or 'not loaded'."""
    _require_darwin()
    result = _launchctl("print", f"gui/{_uid()}/{WATCHER_LABEL}")
    if result.returncode != 0:
        return f"{WATCHER_LABEL}: not loaded"
    lines = [
        ln.strip()
        for ln in result.stdout.splitlines()
        if any(k in ln for k in ("state =", "pid =", "program ="))
    ]
    return "\n".join([WATCHER_LABEL, *lines])
