"""LaunchAgent management for the archive daemons — private machinery.

``archive daemon install`` materializes a LaunchAgent plist from inside the
package — pointing at the installed console script, wherever this Python
environment put it — and loads it. No repo checkout, no Makefile, no sed: this
is the "upgrade to always-fresh" step after a plain ``pip install
thread-archive``, and it is what ``host/Makefile install-agent`` delegates to
for the operator flow (which adds the thread-family manifest on top; the
manifest writer stays in ``host/``, repo-only by design).

Three agents live here:

* the **watcher** (``com.thread-archive.watcher``) — the always-on live-ingest
  process, which also cohosts the read-only web viewer;
* the **MCP server** (``com.thread-archive.mcp``) — one shared, always-on
  streamable-HTTP server all agents connect to, so a single ~3 GB retrieval
  model stays resident instead of one process per connecting client. Without
  it, ``archive-mcp`` runs per-client over stdio (each client its own model).
* the **nightly backup** (``com.thread-archive.backup``) — the scheduled
  durability pipeline (``archive nightly <dest>``: mirror the JSONL truth →
  integrity verify → restore drill), on a daily ``StartCalendarInterval``. Its
  ``dest`` must be a path launchd can reach unattended — a local second disk or
  an already-mounted volume; network shares that drop their mount between runs
  (and the TCC grant a background job needs to touch them) are the operator
  ``host/`` layer's concern (see ``host/run-nightly.sh``), not this builder's.

macOS only, deliberately (launchd is the product's process manager). Each
plist mirrors what its agent needs and nothing else: run at login in the Aqua
session (the archive home and watched stores live in the user's home), logs
under ``<archive home>/logs``.
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
MCP_LABEL = "com.thread-archive.mcp"
BACKUP_LABEL = "com.thread-archive.backup"

# When the nightly-backup agent fires (local time). 04:00 keeps it clear of the
# working day; launchd runs it on wake if the box was asleep at the mark.
BACKUP_DEFAULT_HOUR = 4
BACKUP_DEFAULT_MINUTE = 0

# The shared MCP server's default loopback bind. Adjacent to the watcher's web
# viewer (8787); every client's MCP config points here.
MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 8788


def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise SystemExit("archive daemon: launchd management is macOS-only")


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _entry_path(name: str = "archive") -> Path:
    """A console script this environment installed (e.g. ``archive`` or
    ``archive-mcp``) — the thing the plist must point at (launchd won't inherit
    a venv)."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    raise SystemExit(
        f"archive daemon: cannot find the `{name}` console script next to "
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


def mcp_plist(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
) -> dict:
    """The shared-MCP-server LaunchAgent as a plist dict (pure — no filesystem,
    no launchctl). ``entry`` is the ``archive-mcp`` console script."""
    env = {
        "PATH": f"{entry.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if home:
        env["THREAD_ARCHIVE_HOME"] = home
    return {
        "Label": MCP_LABEL,
        "ProgramArguments": [str(entry), "--http", "--host", host, "--port", str(port)],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        # Always keep it up — many live clients depend on this one server, so
        # restart on any exit, not only a crash. ThrottleInterval caps a crash
        # loop.
        "KeepAlive": True,
        "ThrottleInterval": 5,
        # It serves interactive tool calls; don't let App Nap stall responses.
        "ProcessType": "Standard",
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log_dir / "mcp-stdout.log"),
        "StandardErrorPath": str(log_dir / "mcp-stderr.log"),
        "EnvironmentVariables": env,
    }


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
    """The nightly-backup LaunchAgent as a plist dict (pure — no filesystem, no
    launchctl).

    Runs ``archive nightly <dest>`` on a daily ``StartCalendarInterval`` — the
    whole durability pipeline (mirror the JSONL truth to ``dest`` → integrity
    verify → restore drill) as one scheduled command. ``dest`` must be a path
    launchd can reach unattended at the fire time; the network-share remount is
    the ``host/`` layer's concern, not this builder's (see the module docstring).
    """
    args = [str(entry), "nightly", dest]
    if notify_url:
        args += ["--notify-url", notify_url]
    env = {
        # The entry's own bin dir first so `archive` resolves its interpreter.
        "PATH": f"{entry.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if home:
        env["THREAD_ARCHIVE_HOME"] = home
    return {
        "Label": BACKUP_LABEL,
        "ProgramArguments": args,
        # A scheduled one-shot: fire daily, don't keep it resident. launchd runs
        # a missed fire on the next wake, so a run landing mid-morning (box asleep
        # at the mark) is normal, not a fault.
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "LimitLoadToSessionType": "Aqua",
        # I/O-bound and not latency-sensitive: let it run nice.
        "ProcessType": "Background",
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log_dir / "backup-stdout.log"),
        "StandardErrorPath": str(log_dir / "backup-stderr.log"),
        "EnvironmentVariables": env,
    }


def _uid() -> int:
    import os

    return os.getuid()


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], check=check, capture_output=True, text=True
    )


def _install_agent(label: str, plist_dict: dict, home: Optional[str]) -> Path:
    """Write ``label``'s plist and (re)load the agent. Returns the plist path.

    Idempotent: an existing agent is booted out first, so re-running after an
    upgrade or a config change is the supported way to apply it.
    """
    _require_darwin()
    from ._config import resolve_paths

    log_dir = resolve_paths(home).home / "logs"
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
            f"archive daemon: launchctl bootstrap failed: {result.stderr.strip()}"
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
            f"archive daemon: launchctl kickstart failed: {result.stderr.strip()}"
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


def install_watcher(
    home: Optional[str] = None, *, web: bool = True, web_port: int = 8787
) -> Path:
    """Write the watcher plist and (re)load the agent. Returns the plist path."""
    entry = _entry_path("archive")
    from ._config import resolve_paths

    log_dir = resolve_paths(home).home / "logs"
    return _install_agent(
        WATCHER_LABEL,
        watcher_plist(entry, log_dir, home=home, web=web, web_port=web_port),
        home,
    )


def uninstall_watcher() -> None:
    _uninstall_agent(WATCHER_LABEL)


def restart_watcher() -> None:
    _restart_agent(WATCHER_LABEL)


def watcher_status() -> str:
    return _agent_status(WATCHER_LABEL)


def install_mcp(
    home: Optional[str] = None,
    *,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
) -> Path:
    """Write the shared-MCP-server plist and (re)load the agent. Returns the
    plist path."""
    entry = _entry_path("archive-mcp")
    from ._config import resolve_paths

    log_dir = resolve_paths(home).home / "logs"
    return _install_agent(
        MCP_LABEL, mcp_plist(entry, log_dir, home=home, host=host, port=port), home
    )


def uninstall_mcp() -> None:
    _uninstall_agent(MCP_LABEL)


def restart_mcp() -> None:
    _restart_agent(MCP_LABEL)


def mcp_status() -> str:
    return _agent_status(MCP_LABEL)


def install_backup(
    dest: str,
    home: Optional[str] = None,
    *,
    hour: int = BACKUP_DEFAULT_HOUR,
    minute: int = BACKUP_DEFAULT_MINUTE,
    notify_url: Optional[str] = None,
) -> Path:
    """Write the nightly-backup plist and (re)load the agent. Returns the plist
    path."""
    entry = _entry_path("archive")
    from ._config import resolve_paths

    log_dir = resolve_paths(home).home / "logs"
    return _install_agent(
        BACKUP_LABEL,
        backup_plist(
            entry, log_dir, dest, home=home, hour=hour, minute=minute,
            notify_url=notify_url,
        ),
        home,
    )


def uninstall_backup() -> None:
    _uninstall_agent(BACKUP_LABEL)


def restart_backup() -> None:
    _restart_agent(BACKUP_LABEL)


def backup_status() -> str:
    return _agent_status(BACKUP_LABEL)


def backup_agent_dest() -> Optional[str]:
    """The ``dest`` the installed backup agent runs against, or ``None`` when no
    plist is present or it isn't the package-generated shape (e.g. the ``host/``
    ``run-nightly.sh`` wrapper form, whose dest lives inside the wrapper's args).
    Reads the plist on disk, so it reflects the agent even when it isn't loaded."""
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
