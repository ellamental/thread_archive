"""The Linux (systemd ``--user``) service backend.

Renders an :class:`~.spec.AgentSpec` into systemd user units under
``~/.config/systemd/user/`` and drives the agent's lifecycle through ``systemctl
--user``. The resident agents (watcher, MCP server) are ``.service`` units; the
nightly backup is a ``.service`` fired by a companion ``.timer``.

Two operational details a user unit needs and this backend handles:

* **linger** — ``loginctl enable-linger`` lets the user's units keep running
  after logout and lets the backup timer fire with nobody logged in. It is
  attempted on install and is fail-soft: on a locked-down box it may need admin,
  and the services still run while a session is live.
* **the user bus** — ``systemctl --user`` needs ``$XDG_RUNTIME_DIR``; when it is
  unset (a headless / sudo context) it is pointed at ``/run/user/$(id -u)``.

Log files use ``StandardOutput=append:`` so the archive's ``<home>/logs/*.log``
stay the same files the rest of the system reads (``append:`` is systemd v240+,
present on every supported Ubuntu). The installer mkdirs ``<home>/logs`` first —
systemd won't create an ``append:`` target's parent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    mcp_spec,
    watcher_spec,
)

_BASE = {
    "watcher": "thread-archive-watcher",
    "mcp": "thread-archive-mcp",
    "backup": "thread-archive-backup",
}
_DESC = {
    "watcher": "thread-archive watcher (live ingest + cohosted web viewer)",
    "mcp": "thread-archive shared MCP server (streamable HTTP)",
    "backup": "thread-archive backup nightly (mirror + verify + restore drill)",
}
# The agents whose primary, user-facing unit is a timer (armed, not resident).
_SCHEDULED = frozenset({"backup"})


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _service_name(agent: str) -> str:
    return f"{_BASE[agent]}.service"


def _timer_name(agent: str) -> str:
    return f"{_BASE[agent]}.timer"


def _primary(agent: str) -> str:
    """The unit the operator enables/restarts/queries — the timer for a scheduled
    agent (arming it, not running the job), else the service itself."""
    return _timer_name(agent) if agent in _SCHEDULED else _service_name(agent)


def _unit_files(agent: str) -> list[str]:
    files = [_service_name(agent)]
    if agent in _SCHEDULED:
        files.append(_timer_name(agent))
    return files


def _systemd_path(bin_dir: Path) -> str:
    # The entry's own bin dir first so `archive` resolves its interpreter (a user
    # unit inherits no venv); then the usual Linux locations. No Homebrew here.
    return f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"


def _q(arg: str) -> str:
    """Quote one ExecStart argument for systemd (only when it needs it)."""
    if arg and not any(c.isspace() for c in arg) and '"' not in arg and "\\" not in arg:
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exec(argv: list[str]) -> str:
    return " ".join(_q(a) for a in argv)


def _env_line(key: str, value: str) -> str:
    # Quote the whole assignment when the value carries whitespace so systemd
    # keeps it as one token.
    if any(c.isspace() for c in value):
        return f'Environment="{key}={value}"'
    return f"Environment={key}={value}"


def _service_unit(spec: AgentSpec) -> str:
    lines = ["[Unit]", f"Description={_DESC[spec.name]}"]
    if spec.keep_trying:
        # Never latch into a permanent 'failed' — mirror launchd's respawn-forever
        # (systemd would otherwise give up after StartLimitBurst restarts).
        lines.append("StartLimitIntervalSec=0")
    lines += ["", "[Service]"]
    lines.append("Type=oneshot" if spec.schedule is not None else "Type=simple")
    lines.append(f"ExecStart={_exec(spec.argv)}")
    lines.append(f"WorkingDirectory={spec.working_dir}")
    lines.append(_env_line("PATH", _systemd_path(spec.bin_dir)))
    for key, value in spec.env.items():
        lines.append(_env_line(key, value))
    if spec.restart is Restart.ALWAYS:
        lines += ["Restart=always", f"RestartSec={RESTART_SEC}"]
    elif spec.restart is Restart.ON_FAILURE:
        lines += ["Restart=on-failure", f"RestartSec={RESTART_SEC}"]
    if spec.nice is not None:
        lines.append(f"Nice={spec.nice}")
    if spec.io_idle:
        lines.append("IOSchedulingClass=idle")
    lines.append(f"LimitNOFILE={spec.nofile}")
    lines.append(f"StandardOutput=append:{spec.log_stdout}")
    lines.append(f"StandardError=append:{spec.log_stderr}")
    if spec.run_at_load:
        # A resident agent that should come up at login. A scheduled service is
        # timer-activated instead and carries no [Install].
        lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def _timer_unit(spec: AgentSpec) -> str:
    at = spec.schedule
    assert at is not None
    lines = [
        "[Unit]",
        f"Description={_DESC[spec.name]} schedule",
        "",
        "[Timer]",
        f"OnCalendar=*-*-* {at.hour:02d}:{at.minute:02d}:00",
        # Run a missed fire on the next start (the box was off/asleep at the mark)
        # — the wake-catchup analog of launchd's scheduled-job behavior.
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return "\n".join(lines) + "\n"


def _units(spec: AgentSpec) -> dict[str, str]:
    """Every unit file the agent needs: ``{filename: text}`` (a ``.service``, plus
    a ``.timer`` for a scheduled agent). Pure — no filesystem, no systemctl."""
    out = {_service_name(spec.name): _service_unit(spec)}
    if spec.schedule is not None:
        out[_timer_name(spec.name)] = _timer_unit(spec)
    return out


# Convenience builders mirroring the launchd backend (for tests + direct callers).


def watcher_units(
    entry: Path, log_dir: Path, *, home: Optional[str] = None, web: bool = True,
    web_port: int = 8787, has_viewer: Optional[bool] = None,
) -> dict[str, str]:
    return _units(watcher_spec(entry, log_dir, home=home, web=web, web_port=web_port,
                               has_viewer=has_viewer))


def mcp_units(
    entry: Path, log_dir: Path, *, home: Optional[str] = None, host: str = MCP_DEFAULT_HOST,
    port: int = MCP_DEFAULT_PORT, ingest: bool = False,
) -> dict[str, str]:
    return _units(mcp_spec(entry, log_dir, home=home, host=host, port=port, ingest=ingest))


def backup_units(
    entry: Path, log_dir: Path, dest: str, *, home: Optional[str] = None,
    hour: int = BACKUP_DEFAULT_HOUR, minute: int = BACKUP_DEFAULT_MINUTE,
    notify_url: Optional[str] = None,
) -> dict[str, str]:
    return _units(
        backup_spec(entry, log_dir, dest, home=home, hour=hour, minute=minute,
                    notify_url=notify_url)
    )


# ── systemctl lifecycle ───────────────────────────────────────────────────────


def _sysenv() -> dict[str, str]:
    env = dict(os.environ)
    # `systemctl --user` needs the user bus; point it at the runtime dir when the
    # context (headless / sudo) left it unset.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, env=_sysenv()
    )


def _enable_linger() -> bool:
    """Best-effort ``loginctl enable-linger`` for the current user. Returns
    whether it succeeded; never raises."""
    try:
        return (
            subprocess.run(
                ["loginctl", "enable-linger"], capture_output=True, text=True, env=_sysenv()
            ).returncode
            == 0
        )
    except OSError:
        return False


def _install(spec: AgentSpec) -> Path:
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    # systemd won't create an append: target's parent.
    spec.log_stdout.parent.mkdir(parents=True, exist_ok=True)
    for fname, text in _units(spec).items():
        (d / fname).write_text(text, encoding="utf-8")
    if not _enable_linger():
        print(
            "thread-archive service: could not enable linger — the agent stops at logout "
            f"until you run `sudo loginctl enable-linger {os.environ.get('USER', '$USER')}`.",
            file=sys.stderr,
        )
    _systemctl("daemon-reload")
    primary = _primary(spec.name)
    _systemctl("enable", primary)
    result = _systemctl("restart", primary)
    if result.returncode != 0:
        raise SystemExit(
            f"thread-archive service: systemctl restart {primary} failed: {result.stderr.strip()}"
        )
    return d / primary


def _uninstall(agent: str) -> None:
    _systemctl("disable", "--now", _primary(agent))
    for fname in _unit_files(agent):
        (unit_dir() / fname).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    _systemctl("reset-failed", *_unit_files(agent))


def _restart(agent: str) -> None:
    primary = _primary(agent)
    result = _systemctl("restart", primary)
    if result.returncode != 0:
        raise SystemExit(
            f"thread-archive service: systemctl restart {primary} failed: {result.stderr.strip()}"
        )


def _show(unit: str, *props: str) -> dict[str, str]:
    args = []
    for p in props:
        args += ["-p", p]
    result = _systemctl("show", *args, unit)
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _status(agent: str) -> str:
    unit = _primary(agent)
    props = _show(unit, "LoadState", "ActiveState", "SubState", "MainPID")
    if props.get("LoadState") in (None, "not-found"):
        return f"{unit}: not loaded"
    lines = [unit]
    for key in ("ActiveState", "SubState", "MainPID"):
        if props.get(key):
            lines.append(f"{key} = {props[key]}")
    return "\n".join(lines)


def _is_loaded(agent: str) -> bool:
    return _systemctl("is-active", _primary(agent)).stdout.strip() == "active"


def _read_service_text(agent: str) -> Optional[str]:
    try:
        return (unit_dir() / _service_name(agent)).read_text(encoding="utf-8")
    except OSError:
        return None


def _agent_home(agent: str) -> Optional[str]:
    """The ``THREAD_ARCHIVE_HOME`` the installed unit pins, or ``None`` when it
    sets none / can't be read. Parses the on-disk unit, so it reflects the agent
    even when the manager hasn't loaded it."""
    text = _read_service_text(agent)
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        for prefix in ('Environment="THREAD_ARCHIVE_HOME=', "Environment=THREAD_ARCHIVE_HOME="):
            if line.startswith(prefix):
                value = line[len(prefix):]
                return value[:-1] if value.endswith('"') else value
    return None


def _split_exec(line: str) -> list[str]:
    """Split an ExecStart value into argv, honoring the double-quoting :func:`_q`
    emits."""
    args: list[str] = []
    cur: list[str] = []
    in_q = False
    esc = False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\" and in_q:
            esc = True
        elif ch == '"':
            in_q = not in_q
        elif ch.isspace() and not in_q:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur))
    return args


def backup_agent_dest() -> Optional[str]:
    """The ``dest`` the installed backup unit runs against, or ``None`` when no
    unit is present or its ExecStart isn't the package-generated shape."""
    text = _read_service_text("backup")
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            argv = _split_exec(line[len("ExecStart="):])
            try:
                return argv[argv.index("nightly") + 1]
            except (ValueError, IndexError):
                return None
    return None


# ── the backend ───────────────────────────────────────────────────────────────


class SystemdBackend:
    """The Linux service backend (see :class:`~.base.ServiceBackend`)."""

    name = "systemd"

    def available(self, platform: str) -> bool:
        return platform.startswith("linux") and shutil.which("systemctl") is not None

    def label(self, agent: str) -> str:
        return _primary(agent)

    def render(self, spec: AgentSpec) -> dict[str, str]:
        return _units(spec)

    def install(self, spec: AgentSpec) -> Path:
        return _install(spec)

    def uninstall(self, agent: str) -> None:
        _uninstall(agent)

    def restart(self, agent: str) -> None:
        _restart(agent)

    def status(self, agent: str) -> str:
        return _status(agent)

    def is_installed(self, agent: str) -> bool:
        return (unit_dir() / _service_name(agent)).exists()

    def is_loaded(self, agent: str) -> bool:
        return _is_loaded(agent)

    def agent_home(self, agent: str) -> Optional[str]:
        return _agent_home(agent)

    def backup_dest(self) -> Optional[str]:
        return backup_agent_dest()


_BACKEND = SystemdBackend()
register(_BACKEND)
