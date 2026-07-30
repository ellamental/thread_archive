"""Platform-neutral definitions of the archive's long-running agents.

An :class:`AgentSpec` describes *what* one agent is — its command, environment,
restart policy, schedule, resource limits, log destinations — in **semantic**
terms, never in launchd- or systemd-shaped ones. Each platform backend
(:mod:`.launchd`, :mod:`.systemd`, a future Windows backend) renders a spec into
its own manifest (a plist, a unit file, a scheduled task). This module is the one
place that knows what the watcher / MCP server / nightly backup actually are;
adding a platform means writing a renderer, not re-deriving the agents.

Three agents:

* the **watcher** (``watch``) — the always-on live-ingest process, which also
  cohosts the read-only web viewer;
* the **MCP server** (``--http``) — one shared, always-on streamable-HTTP server
  all agents connect to, so a single retrieval model stays resident instead of
  one process per connecting client;
* the **nightly backup** (``nightly <dest>``) — the scheduled backup pipeline
  (mirror the JSONL truth → integrity verify → restore drill), fired daily. Its
  ``dest`` must be a path the scheduler can reach unattended.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .._config import ENV_HOME, ENV_MCP_INGEST
from .._viewer import viewer_available

# Open-file ceiling for the agents that touch the truth tree. A background job is
# handed a soft limit of 256 descriptors, which the truth log's own append-handle
# cache (``_truth.drain.MAX_OPEN_HANDLES``, 256) is sized to fill on its own —
# leaving nothing for the SQLite db/wal/shm, the vector pack, the locks and the
# logs. A pass that touches many threads at once then fails on ``[Errno 24] Too
# many open files`` partway through, which the fingerprint seam retries forever
# without ever getting further. This is the headroom that cap always assumed.
AGENT_MAX_FILES = 4096

# Minimum respawn interval for a restarting agent (seconds). Paces a crash loop
# without ever giving up (see ``keep_trying``).
RESTART_SEC = 5

# When the nightly-backup agent fires (local time). 04:00 keeps it clear of the
# working day; a scheduler runs a missed fire on wake if the box was asleep at
# the mark.
BACKUP_DEFAULT_HOUR = 4
BACKUP_DEFAULT_MINUTE = 0

# The shared MCP server's default loopback bind. Adjacent to the watcher's web
# viewer (8787); every client's MCP config points here.
MCP_DEFAULT_HOST = "127.0.0.1"
MCP_DEFAULT_PORT = 8788


class Restart(Enum):
    """When a backend should respawn the agent after it exits."""

    NEVER = "never"  # run-to-completion (the scheduled backup)
    ON_FAILURE = "on_failure"  # only on an abnormal exit (the watcher)
    ALWAYS = "always"  # on any exit — many clients depend on it (the MCP server)


@dataclass(frozen=True)
class DailyAt:
    """A daily wall-clock fire time (local)."""

    hour: int
    minute: int


@dataclass(frozen=True)
class AgentSpec:
    """One agent, described in platform-neutral terms.

    ``argv`` is the full command (``[str(entry), "watch", ...]``). ``bin_dir`` is
    the console script's own directory — a backend composes the agent's ``PATH``
    from it (a scheduled process inherits no venv), which is why ``PATH`` is not
    baked into ``env``. ``env`` carries only the archive's own variables
    (``THREAD_ARCHIVE_HOME``, ``THREAD_ARCHIVE_MCP_INGEST``) and is insertion-
    ordered so a rendered manifest is stable.
    """

    name: str  # logical: "watcher" | "mcp" | "backup"
    argv: list[str]
    bin_dir: Path
    working_dir: Path
    log_stdout: Path
    log_stderr: Path
    restart: Restart
    keep_trying: bool  # respawn forever (never latch into a permanent "failed")
    run_at_load: bool  # start at login/boot
    nofile: int
    schedule: Optional[DailyAt] = None
    nice: Optional[int] = None  # process niceness (the I/O-bound backup)
    io_idle: bool = False  # lowest I/O scheduling priority (the backup)
    env: dict[str, str] = field(default_factory=dict)


def entry_path(name: str = "archive") -> Path:
    """A console script this environment installed (e.g. ``archive`` or
    ``archive-mcp``) — the thing a manifest must point at (a scheduled process
    won't inherit this venv)."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    raise SystemExit(
        f"thread-archive service: cannot find the `{name}` console script next to "
        f"{sys.executable} or on PATH — is the package installed in this environment?"
    )


def _home_env(home: Optional[str]) -> dict[str, str]:
    return {ENV_HOME: home} if home else {}


def watcher_spec(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    web: bool = True,
    web_port: int = 8787,
    has_viewer: Optional[bool] = None,
) -> AgentSpec:
    """The always-on watcher (live ingest, plus the viewer where one exists).

    ``web`` is what the caller asked for; ``has_viewer`` is whether there is
    anything to ask for, and defaults to probing this installation.
    """
    if has_viewer is None:
        has_viewer = viewer_available()
    argv = [str(entry), "watch"]
    # The viewer is dev-only and ships in no wheel, so an install must never
    # write a unit carrying a flag its own CLI does not register —
    # launchd/systemd would restart-loop on argparse exit 2. Gated here rather
    # than at each caller's default because this is the one place the request
    # becomes argv.
    if web and has_viewer:
        # Cohost the read-only viewer in the watcher's own process so it has a
        # persistent URL — one process, one engine; WAL makes the concurrent
        # reads safe (see _store/_base.py).
        argv += ["--web", "--web-port", str(web_port)]
    return AgentSpec(
        name="watcher",
        argv=argv,
        bin_dir=entry.parent,
        working_dir=Path.home(),
        log_stdout=log_dir / "watcher-stdout.log",
        log_stderr=log_dir / "watcher-stderr.log",
        restart=Restart.ON_FAILURE,
        keep_trying=True,
        run_at_load=True,
        nofile=AGENT_MAX_FILES,
        env=_home_env(home),
    )


def mcp_spec(
    entry: Path,
    log_dir: Path,
    *,
    home: Optional[str] = None,
    host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT,
    ingest: bool = False,
) -> AgentSpec:
    """The shared MCP server. ``entry`` is the ``archive-mcp`` console script."""
    env = {ENV_MCP_INGEST: "1" if ingest else "0"}
    # The shared server is a retrieval service. The separately installed watcher
    # owns ingestion; a bare MCP process never mutates by surprise.
    env.update(_home_env(home))
    return AgentSpec(
        name="mcp",
        argv=[str(entry), "--http", "--host", host, "--port", str(port)],
        bin_dir=entry.parent,
        working_dir=Path.home(),
        log_stdout=log_dir / "mcp-stdout.log",
        log_stderr=log_dir / "mcp-stderr.log",
        # Always keep it up — many live clients depend on this one server, so
        # restart on any exit, not only a crash.
        restart=Restart.ALWAYS,
        keep_trying=True,
        run_at_load=True,
        nofile=AGENT_MAX_FILES,
        env=env,
    )


def backup_spec(
    entry: Path,
    log_dir: Path,
    dest: str,
    *,
    home: Optional[str] = None,
    hour: int = BACKUP_DEFAULT_HOUR,
    minute: int = BACKUP_DEFAULT_MINUTE,
    notify_url: Optional[str] = None,
) -> AgentSpec:
    """The nightly backup pipeline: ``thread-archive backup nightly <dest>`` on a
    daily schedule — mirror the JSONL truth to ``dest`` → integrity verify →
    restore drill, as one scheduled command. ``dest`` must be a path the
    scheduler can reach unattended at the fire time."""
    # ``dest`` follows ``nightly`` either way, which is what the installed-agent
    # readers key off — so a manifest written before the command grew groups
    # still parses, and this one still parses under the pre-group spelling.
    argv = [str(entry), "backup", "nightly", dest]
    if notify_url:
        argv += ["--notify-url", notify_url]
    return AgentSpec(
        name="backup",
        argv=argv,
        bin_dir=entry.parent,
        working_dir=Path.home(),
        log_stdout=log_dir / "backup-stdout.log",
        log_stderr=log_dir / "backup-stderr.log",
        # A scheduled one-shot: fire on the schedule, don't keep it resident.
        restart=Restart.NEVER,
        keep_trying=False,
        run_at_load=False,
        nofile=AGENT_MAX_FILES,
        schedule=DailyAt(int(hour), int(minute)),
        # I/O-bound and not latency-sensitive: let it run nice and at idle I/O.
        nice=10,
        io_idle=True,
        env=_home_env(home),
    )
