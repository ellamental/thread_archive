"""Branch-coverage tests for the setup/install machinery: the ``_launchd``
LaunchAgent builder + load/unload/restart/status wrappers, the ``_setup.clients``
MCP-client detection/wiring, and the untouched branches of the ``_setup.wizard``
first-run flow.

Nothing here touches real launchctl, the real ``~/Library/LaunchAgents``, or a
real ``claude`` CLI. ``$PATH`` is pinned per test (autouse) to a directory
holding only the stand-in executables that test wrote, so a ``launchctl`` or
``claude`` the product spawns by name can only ever reach a stand-in — the real
``_launchctl`` / ``_run`` bodies run, over real argv, real exit codes and real
stdout parsing, and "did it run?" is answered by reading the log the stand-in
appended to. ``$HOME`` is redirected per test so plist writes land in
``tmp_path``, and the wizard is handed a :class:`FakeMachine` through its
``machine`` seam so no step can schedule anything on this box.

Complements ``test_launchd.py`` (pure-dict plist shape) and
``test_setup_wizard.py`` (config persistence + the non-TTY safety stance).
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from thread_archive import _launchd
from thread_archive._launchd import BACKUP_LABEL, MCP_LABEL, WATCHER_LABEL
from thread_archive._ops.health import record_health
from thread_archive._setup import clients, wizard
from thread_archive._setup import machine as machine_mod
from thread_archive._setup.machine import Machine
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.lazy import acquire_ingest_owner

# ── stand-in executables ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def stub_bin(tmp_path, monkeypatch) -> Path:
    """``$PATH``, pinned to a directory that starts out empty.

    Every test in this module gets it, so a subprocess the product spawns by
    name resolves to a stand-in the test wrote — or to nothing at all. The
    operator's real launchctl and real ``claude`` config stay out of reach even
    from a test that writes no stand-in.
    """
    b = tmp_path / "stub-bin"
    b.mkdir()
    monkeypatch.setenv("PATH", str(b))
    return b


def _write_exe(path: Path, script: str) -> Path:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _arms(results: dict[str, tuple[int, str, str]]) -> list[str]:
    """``case`` arms: pattern → echo the scripted stdout/stderr, exit the code."""
    arms = []
    for pattern, (rc, out, err) in results.items():
        body = []
        if out:
            body.append(f'echo "{out}"')
        if err:
            body.append(f'echo "{err}" >&2')
        body.append(f"exit {rc}")
        arms.append(f'  "{pattern}") {"; ".join(body)} ;;')
    return arms


def _launchctl_stub(
    stub_bin: Path, results: Optional[dict[str, tuple[int, str, str]]] = None
) -> Path:
    """A ``launchctl`` stand-in on PATH, scripted per subcommand
    (``{subcommand: (returncode, stdout, stderr)}``; anything unlisted exits 0).

    Returns the log it appends one line of arguments to per invocation.
    """
    log = stub_bin.parent / "launchctl.log"
    script = "\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        'case "$1" in',
        *_arms(results or {}),
        "  *) exit 0 ;;",
        "esac",
    ]) + "\n"
    _write_exe(stub_bin / "launchctl", script)
    return log


def _claude_stub(
    stub_bin: Path, results: Optional[dict[str, tuple[int, str, str]]] = None
) -> Path:
    """A ``claude`` CLI stand-in on PATH, scripted per subcommand pair
    (``{"mcp get": (rc, stdout, stderr), ...}``). Returns its argv log."""
    log = stub_bin.parent / "claude.log"
    script = "\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        'case "$1 $2" in',
        *_arms(results or {}),
        "  *) exit 0 ;;",
        "esac",
    ]) + "\n"
    _write_exe(stub_bin / "claude", script)
    return log


def _calls(log: Path) -> list[list[str]]:
    """Every invocation the stand-in recorded, as its argument list."""
    if not log.exists():
        return []
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines() if line]


def _subcommands(log: Path) -> list[str]:
    return [c[0] for c in _calls(log)]


def _force_platform(monkeypatch, module, platform: str) -> None:
    """The platform seam this file cannot inject through: ``_launchd``'s macOS
    gate is a module-level ``sys.platform`` read behind module functions, and
    both sides of it must be provable on either kind of host. The wizard's own
    gate needs none of this — it asks its :class:`Machine`."""
    monkeypatch.setattr(module.sys, "platform", platform)


def _darwin(monkeypatch, module=_launchd) -> None:
    _force_platform(monkeypatch, module, "darwin")


class _MacMachine(Machine):
    """The real host probes with the macOS gate answered yes, so the
    agent-covers-this-home logic is exercised on any kind of host."""

    macos = True


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeMachine:
    """A host to run the wizard against: what is already scheduled on it, what
    this install has, and a record of everything setup asked it to install.

    Hand-written stand-in for :class:`~thread_archive._setup.machine.Machine`,
    handed to the flow through its ``machine`` parameter.
    """

    def __init__(
        self, *, macos: bool = True, watcher: bool = False, backup: bool = False,
        curation: bool = False, backup_dest: Optional[str] = None,
        curation_installed: bool = False, embeddings: bool = True,
        install_fails: Optional[str] = None,
    ):
        self.macos = macos
        self._watcher, self._backup, self._curation = watcher, backup, curation
        self._backup_dest = backup_dest
        self._curation_installed = curation_installed
        self._embeddings = embeddings
        self._install_fails = install_fails
        self.installed: list[tuple] = []

    def watcher_running(self, home=None) -> bool:
        return self._watcher

    def backup_running(self, home=None) -> bool:
        return self._backup

    def backup_dest(self) -> Optional[str]:
        return self._backup_dest

    def curation_running(self, home=None) -> bool:
        return self._curation

    def curation_installed(self) -> bool:
        return self._curation_installed

    def embeddings_installed(self) -> bool:
        return self._embeddings

    def install_watcher(self, home=None) -> None:
        if self._install_fails:
            raise SystemExit(self._install_fails)
        self.installed.append(("watcher", home))

    def install_backup(self, dest, home=None) -> None:
        if self._install_fails:
            raise SystemExit(self._install_fails)
        self.installed.append(("backup", dest, home))


class FakeWatcher(SourceWatcher):
    """A discoverable/importable source with no real store behind it."""

    def __init__(self, name: str, *, available=True, items=2, events=4,
                 discover_raises=False, poll_raises=False, poll_errors=None):
        self._name, self._available = name, available
        self._items, self._events = items, events
        self._discover_raises = discover_raises
        self._poll_raises = poll_raises
        self._poll_errors = poll_errors or []
        self.polled = 0

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def discover(self) -> SourceDiscovery:
        if self._discover_raises:
            raise RuntimeError("store is corrupt")
        now = time.time()
        return SourceDiscovery(
            name=self._name, available=self._available, items=self._items,
            bytes=4096, earliest=now - 86400, latest=now,
        )

    def poll(self) -> WatchResult:
        self.polled += 1
        if self._poll_raises:
            raise RuntimeError("poll blew up")
        return WatchResult(
            sources_checked=1, items_imported=self._items,
            events_created=self._events, errors=list(self._poll_errors),
        )


def _args(*argv: str):
    return wizard.build_parser().parse_args(list(argv))


# ══════════════════════════════════════════════════════════════════════════════
# _launchd
# ══════════════════════════════════════════════════════════════════════════════


def test_require_darwin_gate(monkeypatch) -> None:
    # Forced on-mac the gate is a no-op; forced off-mac it aborts (next test).
    _darwin(monkeypatch)
    _launchd._require_darwin()  # does not raise on darwin


def test_require_darwin_raises_off_mac(monkeypatch) -> None:
    _force_platform(monkeypatch, _launchd, "linux")
    with pytest.raises(SystemExit, match="macOS-only"):
        _launchd._require_darwin()


def test_entry_path_next_to_executable() -> None:
    # The interpreter running this suite is, by definition, a file next to
    # itself — the is_file() branch over the real install layout.
    assert _launchd._entry_path(Path(sys.executable).name) == Path(sys.executable)


def test_entry_path_falls_back_to_which(stub_bin) -> None:
    # A script this environment did not install next to its interpreter, but
    # which PATH has.
    _write_exe(stub_bin / "archive-elsewhere", "#!/bin/sh\n")
    assert _launchd._entry_path("archive-elsewhere") == stub_bin / "archive-elsewhere"


def test_entry_path_missing_script_raises() -> None:
    # Not next to the interpreter, not on PATH (which holds only the stub dir).
    with pytest.raises(SystemExit, match="cannot find the `archive-absent` console script"):
        _launchd._entry_path("archive-absent")


def test_mcp_plist_shape() -> None:
    entry, log_dir = Path("/env/bin/archive-mcp"), Path("/arc/logs")
    p = _launchd.mcp_plist(entry, log_dir, home="/data/arc", host="0.0.0.0", port=9999)
    assert p["Label"] == MCP_LABEL
    assert p["ProgramArguments"] == [
        str(entry), "--http", "--host", "0.0.0.0", "--port", "9999"
    ]
    assert p["KeepAlive"] is True  # every client depends on it: restart on any exit
    assert p["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert p["StandardOutPath"] == str(log_dir / "mcp-stdout.log")
    # Default host/port and no home leave the env var unset.
    d = _launchd.mcp_plist(entry, log_dir)
    assert "THREAD_ARCHIVE_HOME" not in d["EnvironmentVariables"]
    assert d["ProgramArguments"][2:] == ["--host", "127.0.0.1", "--port", "8788"]


def test_truth_touching_agents_raise_the_open_file_limit() -> None:
    """launchd's 256-descriptor default is smaller than the truth log's own
    append-handle cache, so an agent that writes many threads in one pass runs
    out of descriptors mid-pass. Every agent that touches the truth tree must
    clear that cache with room to spare for the db, locks and logs."""
    from thread_archive._truth.drain import MAX_OPEN_HANDLES

    entry, log_dir = Path("/env/bin/archive"), Path("/arc/logs")
    plists = [
        _launchd.watcher_plist(entry, log_dir),
        _launchd.mcp_plist(Path("/env/bin/archive-mcp"), log_dir),
        _launchd.backup_plist(entry, log_dir, dest="/backups"),
    ]
    for p in plists:
        limit = p["SoftResourceLimits"]["NumberOfFiles"]
        assert limit > MAX_OPEN_HANDLES, f"{p['Label']} caps files at {limit}"


def test_install_watcher_writes_plist_and_loads(tmp_path, monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # Path.home() → tmp plist dir
    log = _launchctl_stub(stub_bin, {"bootout": (0, "", ""), "bootstrap": (0, "", "")})
    # The settle after a successful bootout is real seconds of wall clock; the
    # sleep itself is the one thing here that cannot be driven for real.
    slept: list[float] = []
    monkeypatch.setattr(_launchd.time, "sleep", slept.append)

    arc = str(tmp_path / "arc")
    plist_path = _launchd.install_watcher(arc)

    assert plist_path == _launchd._plist_path(WATCHER_LABEL)
    assert plist_path.exists()
    written = plistlib.loads(plist_path.read_bytes())
    assert written["Label"] == WATCHER_LABEL
    assert written["ProgramArguments"][0].endswith("/archive")  # this env's console script
    assert written["ProgramArguments"][1] == "watch"
    assert "--web" in written["ProgramArguments"]
    assert written["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == arc
    # bootout (existing agent) → settle → bootstrap, in that order.
    assert _subcommands(log) == ["bootout", "bootstrap"]
    assert _calls(log)[1][1:] == [f"gui/{_launchd._uid()}", str(plist_path)]
    assert slept == [3]
    assert (Path(arc) / "logs").is_dir()  # log dir created for StandardOutPath


def test_install_mcp_bootstrap_failure_raises_and_skips_settle(
    tmp_path, monkeypatch, stub_bin
) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # bootout returns nonzero (nothing was loaded) → no settle sleep; bootstrap fails.
    _launchctl_stub(stub_bin, {
        "bootout": (1, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5"),
    })

    t0 = time.monotonic()
    with pytest.raises(SystemExit, match="bootstrap failed: Bootstrap failed: 5"):
        _launchd.install_mcp(str(tmp_path / "arc"))
    # No settle was slept: the real 3s wait would still be running.
    assert time.monotonic() - t0 < 3


def test_install_backup_writes_schedule(tmp_path, monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin, {"bootout": (1, "", ""), "bootstrap": (0, "", "")})

    plist_path = _launchd.install_backup("/Volumes/Backup/arc", str(tmp_path / "arc"),
                                         hour=2, minute=15)
    written = plistlib.loads(plist_path.read_bytes())
    assert written["Label"] == BACKUP_LABEL
    assert written["ProgramArguments"][-2:] == ["nightly", "/Volumes/Backup/arc"]
    assert written["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}


def test_uninstall_agent_boots_out_and_removes_plist(tmp_path, monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    log = _launchctl_stub(stub_bin)
    plist = _launchd._plist_path(WATCHER_LABEL)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"stale")

    _launchd.uninstall_watcher()
    assert not plist.exists()
    assert _subcommands(log) == ["bootout"]
    assert _calls(log)[0][1] == f"gui/{_launchd._uid()}/{WATCHER_LABEL}"


def test_uninstall_agent_missing_plist_is_ok(tmp_path, monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _launchctl_stub(stub_bin)
    _launchd.uninstall_mcp()  # unlink(missing_ok=True): no such plist, no raise


def test_restart_agent_kickstarts(monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    log = _launchctl_stub(stub_bin, {"kickstart": (0, "", "")})
    _launchd.restart_mcp()
    assert _calls(log) == [["kickstart", "-k", f"gui/{_launchd._uid()}/{MCP_LABEL}"]]


def test_restart_agent_failure_raises(monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    _launchctl_stub(stub_bin, {"kickstart": (1, "", "No such process")})
    with pytest.raises(SystemExit, match="kickstart failed: No such process"):
        _launchd.restart_backup()


def test_remaining_wrappers_delegate(tmp_path, monkeypatch, stub_bin) -> None:
    # The watcher/backup twins of the wrappers exercised above route through the
    # same helpers; pin them so the whole delegation set is covered.
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    log = _launchctl_stub(stub_bin, {"kickstart": (0, "", ""), "print": (1, "", "")})
    _launchd.restart_watcher()
    _launchd.uninstall_backup()
    assert _launchd.backup_status() == f"{BACKUP_LABEL}: not loaded"
    assert _subcommands(log) == ["kickstart", "bootout", "print"]


def test_agent_status_not_loaded(monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    _launchctl_stub(stub_bin, {"print": (1, "", "")})
    assert _launchd.watcher_status() == f"{WATCHER_LABEL}: not loaded"


def test_agent_status_parses_fields(monkeypatch, stub_bin) -> None:
    _darwin(monkeypatch)
    stdout = (
        f"{MCP_LABEL} = {{\n"
        "state = running\n"
        "pid = 4242\n"
        "program = /env/bin/archive-mcp\n"
        "active count = 1\n"  # dropped: not one of the kept keys
        "}"
    )
    _launchctl_stub(stub_bin, {"print": (0, stdout, "")})
    out = _launchd.mcp_status()
    assert out.splitlines()[0] == MCP_LABEL
    assert "state = running" in out
    assert "pid = 4242" in out
    assert "program = /env/bin/archive-mcp" in out
    assert "active count" not in out


def _write_backup_plist(tmp_path, monkeypatch, args: list[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plist = _launchd._plist_path(BACKUP_LABEL)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": BACKUP_LABEL, "ProgramArguments": args}))


def test_backup_agent_dest_reads_nightly_arg(tmp_path, monkeypatch) -> None:
    _darwin(monkeypatch)
    _write_backup_plist(tmp_path, monkeypatch,
                        ["/env/bin/archive", "nightly", "/Volumes/B/arc"])
    assert _launchd.backup_agent_dest() == "/Volumes/B/arc"


def test_backup_agent_dest_none_without_nightly(tmp_path, monkeypatch) -> None:
    # An external wrapper-script shape: no bare `nightly` arg → unknown dest.
    _darwin(monkeypatch)
    _write_backup_plist(tmp_path, monkeypatch, ["/somewhere/run-nightly.sh"])
    assert _launchd.backup_agent_dest() is None


def test_backup_agent_dest_none_when_plist_absent(tmp_path, monkeypatch) -> None:
    _darwin(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    assert _launchd.backup_agent_dest() is None


def test_backup_agent_dest_none_off_mac(monkeypatch) -> None:
    _force_platform(monkeypatch, _launchd, "linux")
    assert _launchd.backup_agent_dest() is None


# ══════════════════════════════════════════════════════════════════════════════
# _setup.clients
# ══════════════════════════════════════════════════════════════════════════════


def test_console_script_next_to_executable() -> None:
    # This environment's own interpreter: a console script next to itself.
    assert clients.console_script(Path(sys.executable).name) == sys.executable


def test_console_script_falls_back_to_which(stub_bin) -> None:
    _write_exe(stub_bin / "archive-mcp-elsewhere", "#!/bin/sh\n")
    assert clients.console_script("archive-mcp-elsewhere") == str(
        stub_bin / "archive-mcp-elsewhere"
    )


def test_console_script_bare_name_last_resort() -> None:
    # Neither next to the interpreter nor on PATH: still a valid MCP command
    # for a client whose own PATH has it.
    assert clients.console_script("archive-mcp-absent") == "archive-mcp-absent"


def test_claude_cli_uses_which(stub_bin) -> None:
    cli = _write_exe(stub_bin / "claude", "#!/bin/sh\n")
    assert clients.claude_cli() == str(cli)
    cli.unlink()
    assert clients.claude_cli() is None


def test_claude_server_report_wired_and_absent(monkeypatch, stub_bin) -> None:
    monkeypatch.delenv(clients.ENV_HOME, raising=False)  # target = the default home
    log = _claude_stub(stub_bin, {
        "mcp get": (0, "thread-archive:\n  Scope: User\n  Status: Connected", ""),
    })
    cli = str(stub_bin / "claude")
    assert clients.claude_server_report(cli) == (True, None)
    assert _calls(log)[0] == ["mcp", "get", "thread-archive"]

    _claude_stub(stub_bin, {"mcp get": (1, "", "")})  # no such server entry
    assert clients.claude_server_report(cli) == (False, None)


def test_claude_server_report_pending_approval_is_not_wired(stub_bin) -> None:
    out = ("thread-archive:\n  Scope: Project config (shared via .mcp.json)\n"
           "  Status: Pending approval (run 'claude' to approve)")
    _claude_stub(stub_bin, {"mcp get": (0, out, "")})
    wired, problem = clients.claude_server_report(str(stub_bin / "claude"))
    assert wired is False
    assert "pending approval" in problem


def test_claude_server_report_home_mismatch(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.delenv(clients.ENV_HOME, raising=False)  # target = the default home
    cli = str(stub_bin / "claude")
    out = (f"thread-archive:\n  Scope: User\n  Environment:\n"
           f"    THREAD_ARCHIVE_HOME={tmp_path}/other")
    _claude_stub(stub_bin, {"mcp get": (0, out, "")})
    # Entry pinned to another home does not cover the default home…
    wired, problem = clients.claude_server_report(cli)
    assert wired is False and "serves" in problem
    # …but does cover that home when it is the one being set up.
    assert clients.claude_server_report(cli, home=f"{tmp_path}/other") == (True, None)
    # An env-less entry serves the default home, not a custom one.
    _claude_stub(stub_bin, {"mcp get": (0, "thread-archive:\n  Scope: User", "")})
    wired, problem = clients.claude_server_report(cli, home=f"{tmp_path}/mine")
    assert wired is False and "serves" in problem
    assert clients.claude_server_report(cli) == (True, None)


def test_wire_claude_and_config_block_carry_custom_home(tmp_path, monkeypatch, stub_bin) -> None:
    log = _claude_stub(stub_bin)
    cli = str(stub_bin / "claude")
    home = str(tmp_path / "mine")
    assert clients.wire_claude(cli, home=home) == []
    argv = _calls(log)[0]
    assert "--env" in argv
    assert f"THREAD_ARCHIVE_HOME={home}" in argv
    assert argv[-1].endswith("archive-mcp")  # this env's console script, by path
    entry = json.loads(clients.mcp_config_block(home))["mcpServers"]["thread-archive"]
    assert entry["env"] == {"THREAD_ARCHIVE_HOME": home}
    # The default home needs no env pin.
    monkeypatch.delenv(clients.ENV_HOME, raising=False)
    log.unlink()
    assert clients.wire_claude(cli) == []
    assert "--env" not in _calls(log)[0]
    assert "env" not in json.loads(clients.mcp_config_block())["mcpServers"]["thread-archive"]


def test_claude_server_report_probe_failure_reads_unwired(tmp_path, monkeypatch) -> None:
    # A CLI path that cannot be executed at all (old install, wrong path).
    assert clients.claude_server_report(str(tmp_path / "no-such-claude")) == (False, None)

    # A CLI that never answers. The 30s production timeout is real wall clock,
    # so the raise is the one thing here that stands in for the boundary.
    def raise_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, clients._SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(clients.subprocess, "run", raise_timeout)
    assert clients.claude_server_report("/bin/claude") == (False, None)


def test_wire_claude_success_adds_read_server(stub_bin) -> None:
    log = _claude_stub(stub_bin)
    assert clients.wire_claude(str(stub_bin / "claude")) == []
    # Read server only — the librarian write server is the plugin's to wire.
    calls = _calls(log)
    assert len(calls) == 1
    assert calls[0][:5] == ["mcp", "add", "--scope", "user", "thread-archive"]
    assert calls[0][-1].endswith("archive-mcp")


def test_wire_claude_reports_subprocess_errors(tmp_path) -> None:
    errors = clients.wire_claude(str(tmp_path / "no-such-claude"))
    assert len(errors) == 1
    assert "thread-archive:" in errors[0] and "no-such-claude" in errors[0]


def test_wire_claude_uses_stderr_detail_and_fallback(stub_bin) -> None:
    _claude_stub(stub_bin, {"mcp add": (1, "", "usage: claude mcp\nboom detail")})
    cli = str(stub_bin / "claude")
    assert clients.wire_claude(cli) == ["thread-archive: boom detail"]

    _claude_stub(stub_bin, {"mcp add": (1, "", "")})  # no detail → generic message
    assert clients.wire_claude(cli) == ["thread-archive: claude mcp add failed"]


# ══════════════════════════════════════════════════════════════════════════════
# _setup.wizard — formatting + prompt helpers
# ══════════════════════════════════════════════════════════════════════════════


def _reads(*answers: str):
    """A prompt reader that hands back ``answers`` in order."""
    it = iter(answers)
    return lambda prompt: next(it)


def _eof(prompt):
    raise EOFError


def test_fmt_when_yesterday_and_old() -> None:
    now = time.time()
    assert wizard._fmt_when(now - 30 * 3600) == "yesterday"  # 24–48h band
    old = time.mktime(time.strptime("2020-06-15", "%Y-%m-%d"))
    assert wizard._fmt_when(old) == "Jun 2020"  # >48h → month/year


def test_ask_interactive_reads_lowers_and_defaults() -> None:
    assert wizard._ask("q> ", default="d", interactive=True, read=_reads("  YeS ")) == "yes"
    assert wizard._ask("q> ", default="d", interactive=True, read=_reads("")) == "d"
    assert wizard._ask("q> ", default="d", interactive=True, read=_eof) == "d"
    # Non-interactive never reads input at all.
    assert wizard._ask("q> ", default="d", interactive=False, read=_eof) == "d"


def test_ask_path_preserves_case_and_handles_eof() -> None:
    assert wizard._ask_path(
        "q> ", interactive=True, read=_reads("  /Some/Mixed/Path ")
    ) == "/Some/Mixed/Path"
    assert wizard._ask_path("q> ", interactive=True, read=_eof) == ""
    assert wizard._ask_path("q> ", interactive=False, read=_eof) == ""


# ── run_setup: discover + import branches ─────────────────────────────────────


def test_discover_exception_marks_source_absent(archive_home, capsys) -> None:
    good = FakeWatcher("claude-code")
    broken = FakeWatcher("cursor", discover_raises=True)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp"),
        watchers=[good, broken], machine=FakeMachine(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "not found: Cursor" in out  # a discover() that raised reads as absent
    assert good.polled == 1


def test_import_skipped_when_owner_lock_unavailable(archive_home, capsys) -> None:
    # Hold the real ingest-owner lock, as the watcher daemon does for its whole
    # lifetime: the manual import must stand down rather than ingest alongside.
    fd = acquire_ingest_owner()
    assert fd is not None
    try:
        fake = FakeWatcher("claude-code")
        rc = wizard.run_setup(
            _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp"),
            watchers=[fake], machine=FakeMachine(),
        )
    finally:
        os.close(fd)
    assert rc == 0
    assert fake.polled == 0  # another watcher owns ingest → manual import stands down
    assert "already ingests for this archive" in capsys.readouterr().out


def test_import_poll_failure_is_reported(archive_home, capsys) -> None:
    # A source whose poll raises: the failure is captured, no maintain() runs
    # (zero events), and the error summary is printed.
    boom = FakeWatcher("claude-code", poll_raises=True)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp"),
        watchers=[boom], machine=FakeMachine(),
    )
    assert rc == 0
    assert boom.polled == 1
    out = capsys.readouterr().out
    assert "failed: poll blew up" in out
    assert "1 item(s) could not be imported" in out


def test_setup_reports_web_viewer_and_missing_embeddings(archive_home, capsys) -> None:
    machine = FakeMachine(embeddings=False)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import", "--skip-backup", "--skip-mcp"),
        watchers=[], machine=machine,
    )
    assert rc == 0
    assert machine.installed == [("watcher", None)]
    out = capsys.readouterr().out
    assert "web viewer:       http://127.0.0.1:8787" in out  # watcher == launchd
    assert "semantic search:  not installed" in out


# ── _offer_watcher ────────────────────────────────────────────────────────────


def test_offer_watcher_skip_flag(archive_home, capsys) -> None:
    machine = FakeMachine()
    assert wizard._offer_watcher(_args("setup", "--skip-watcher"), False, machine) == "skipped"
    assert "Watcher skipped" in capsys.readouterr().out
    assert machine.installed == []


def test_offer_watcher_non_darwin_unavailable(archive_home) -> None:
    assert wizard._offer_watcher(
        _args("setup"), False, FakeMachine(macos=False)
    ) == "unavailable"


def test_offer_watcher_already_running(archive_home, capsys) -> None:
    machine = FakeMachine(watcher=True)
    assert wizard._offer_watcher(_args("setup"), False, machine) == "already-running"
    assert "already installed and running" in capsys.readouterr().out
    assert machine.installed == []  # never re-installed over the running agent


def test_offer_watcher_installs(archive_home, capsys) -> None:
    machine = FakeMachine()
    # interactive=False → _ask returns the "" default → install.
    assert wizard._offer_watcher(_args("setup", "--yes"), False, machine) == "launchd"
    assert machine.installed == [("watcher", None)]
    assert "Installed" in capsys.readouterr().out


def test_offer_watcher_skip_answer(archive_home, capsys) -> None:
    machine = FakeMachine()
    assert wizard._offer_watcher(
        _args("setup"), True, machine,
        ask=lambda prompt, *, default, interactive: "s",
    ) == "skipped"
    assert machine.installed == []  # must not install on skip
    assert "lazy catch-up covers freshness" in capsys.readouterr().out


def test_offer_watcher_install_failure(archive_home, capsys) -> None:
    machine = FakeMachine(install_fails="launchctl bootstrap failed")
    assert wizard._offer_watcher(_args("setup", "--yes"), False, machine) == "failed"
    assert "Could not install the watcher" in capsys.readouterr().out


# ── _offer_backup: the two untouched branches ─────────────────────────────────


def test_offer_backup_relative_dest_is_resolved(archive_home) -> None:
    machine = FakeMachine()
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "rel/backups"), False, machine
    )
    assert out["status"] == "launchd"
    assert Path(out["dest"]).is_absolute()  # a relative dest was resolved to absolute
    assert out["dest"].endswith("rel/backups")
    assert machine.installed == [("backup", out["dest"], None)]


def test_offer_backup_install_failure(archive_home, capsys) -> None:
    machine = FakeMachine(install_fails="launchctl bootstrap failed")
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "/Volumes/B/arc"), False, machine
    )
    assert out == {"status": "failed"}
    assert "Could not schedule backup" in capsys.readouterr().out


# ── _offer_mcp ────────────────────────────────────────────────────────────────


def test_offer_mcp_skip_flag(archive_home, capsys) -> None:
    assert wizard._offer_mcp(_args("setup", "--skip-mcp"), False) == "skipped"
    assert "MCP wiring skipped" in capsys.readouterr().out


def test_offer_mcp_no_client_prints_config(archive_home, capsys) -> None:
    # PATH holds no `claude`.
    assert wizard._offer_mcp(_args("setup"), False) == "printed"
    out = capsys.readouterr().out
    assert "no supported client CLI found" in out
    assert "mcpServers" in out


def test_offer_mcp_already_wired(archive_home, monkeypatch, stub_bin, capsys) -> None:
    monkeypatch.delenv(clients.ENV_HOME, raising=False)  # target = the default home
    _claude_stub(stub_bin, {"mcp get": (0, "thread-archive:\n  Scope: User", "")})
    assert wizard._offer_mcp(_args("setup"), False) == "already-wired"
    assert "already has the archive's MCP server" in capsys.readouterr().out


def test_offer_mcp_print_only_answer(archive_home, stub_bin, capsys) -> None:
    log = _claude_stub(stub_bin, {"mcp get": (1, "", "")})  # not wired yet
    assert wizard._offer_mcp(
        _args("setup"), True,
        ask=lambda prompt, *, default, interactive: "p",
    ) == "printed"
    assert "mcpServers" in capsys.readouterr().out
    assert [c[1] for c in _calls(log)] == ["get"]  # probed only — must not wire


def test_offer_mcp_skip_answer(archive_home, stub_bin, capsys) -> None:
    log = _claude_stub(stub_bin, {"mcp get": (1, "", "")})
    assert wizard._offer_mcp(
        _args("setup"), True,
        ask=lambda prompt, *, default, interactive: "s",
    ) == "skipped"
    assert "thread_archive setup" in capsys.readouterr().out
    assert [c[1] for c in _calls(log)] == ["get"]  # probed only, never added


def test_offer_mcp_wires_successfully(archive_home, stub_bin, capsys) -> None:
    log = _claude_stub(stub_bin, {"mcp get": (1, "", ""), "mcp add": (0, "", "")})
    assert wizard._offer_mcp(_args("setup", "--yes"), False) == "wired"
    assert [c[1] for c in _calls(log)] == ["get", "add"]
    assert "Wired:" in capsys.readouterr().out


def test_offer_mcp_wiring_failure(archive_home, stub_bin, capsys) -> None:
    _claude_stub(stub_bin, {"mcp get": (1, "", ""), "mcp add": (1, "", "nope")})
    assert wizard._offer_mcp(_args("setup", "--yes"), False) == "failed"
    out = capsys.readouterr().out
    assert "Wiring hit trouble" in out
    assert "! thread-archive: nope" in out
    assert "mcpServers" in out  # …and the manual config, so wiring is still possible


# ── Machine: which agents cover this home ─────────────────────────────────────


def _install_plist(monkeypatch, tmp_path, label, home):
    """A real agent plist where ``_plist_path`` resolves it: redirect ``$HOME``
    to tmp and write ``Library/LaunchAgents/<label>.plist`` there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    env = {"THREAD_ARCHIVE_HOME": home} if home else {}
    (agents / f"{label}.plist").write_bytes(
        plistlib.dumps({"Label": label, "EnvironmentVariables": env})
    )


def test_watcher_running_non_darwin(monkeypatch) -> None:
    _force_platform(monkeypatch, machine_mod, "linux")
    assert Machine().watcher_running() is False


def test_watcher_running_not_loaded(stub_bin) -> None:
    _launchctl_stub(stub_bin, {"print": (1, "", "")})
    assert _MacMachine().watcher_running() is False


def test_watcher_running_matches_this_home(tmp_path, monkeypatch, stub_bin) -> None:
    _launchctl_stub(stub_bin, {"print": (0, "", "")})
    agent_home = str(tmp_path / "agent")
    _install_plist(monkeypatch, tmp_path, WATCHER_LABEL, agent_home)
    assert _MacMachine().watcher_running(agent_home) is True
    assert _MacMachine().watcher_running(str(tmp_path / "elsewhere")) is False


def test_watcher_running_unreadable_plist_assumes_default(tmp_path, monkeypatch, stub_bin) -> None:
    _launchctl_stub(stub_bin, {"print": (0, "", "")})
    monkeypatch.setenv("HOME", str(tmp_path))  # no plist under this home
    monkeypatch.delenv("THREAD_ARCHIVE_HOME", raising=False)
    assert _MacMachine().watcher_running() is True  # loaded, plist unreadable → default
    # …and default wiring never covers a non-default home.
    assert _MacMachine().watcher_running(str(tmp_path / "custom")) is False


def test_backup_running_non_darwin(monkeypatch) -> None:
    _force_platform(monkeypatch, machine_mod, "linux")
    assert Machine().backup_running() is False


def test_backup_running_not_loaded(stub_bin) -> None:
    _launchctl_stub(stub_bin, {"print": (1, "", "")})
    assert _MacMachine().backup_running() is False


def test_backup_running_no_home_env_reads_as_default(tmp_path, monkeypatch, stub_bin) -> None:
    # The host/ install shape: a plist with no home env is read as the default
    # wiring, so it counts as covering the default home.
    _launchctl_stub(stub_bin, {"print": (0, "", "")})
    _install_plist(monkeypatch, tmp_path, BACKUP_LABEL, None)
    monkeypatch.delenv("THREAD_ARCHIVE_HOME", raising=False)
    assert _MacMachine().backup_running() is True
    # The process env is not evidence of the agent's home: even with
    # $THREAD_ARCHIVE_HOME pinned to a custom home (open_archive does this),
    # the env-less agent still reads as covering only the default home.
    custom = tmp_path / "custom"
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(custom))
    assert _MacMachine().backup_running(str(custom)) is False


def test_backup_running_unreadable_plist_assumes_default(tmp_path, monkeypatch, stub_bin) -> None:
    _launchctl_stub(stub_bin, {"print": (0, "", "")})
    monkeypatch.setenv("HOME", str(tmp_path))  # no plist under this home
    monkeypatch.delenv("THREAD_ARCHIVE_HOME", raising=False)
    assert _MacMachine().backup_running() is True


def test_curation_running_needs_both_drains(tmp_path, monkeypatch, stub_bin) -> None:
    # A half-installed pair reads as not running.
    _launchctl_stub(stub_bin, {"print": (0, "", "")})
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    arc = str(tmp_path / "arc")
    for label in (machine_mod.LIBRARIAN_LABEL, machine_mod.GARDENER_LABEL):
        assert _MacMachine().curation_running(arc) is False
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(
            {"Label": label, "EnvironmentVariables": {"THREAD_ARCHIVE_HOME": arc}}
        ))
    assert _MacMachine().curation_running(arc) is True


def test_machine_reads_this_installs_optional_packages() -> None:
    # Both are importability questions about this environment, answered against
    # whatever it actually has — the gate on recommending a command or a search
    # mode the machine doesn't carry.
    assert Machine().curation_installed() is (
        importlib.util.find_spec("thread_librarian") is not None
    )
    assert Machine().embeddings_installed() is (
        importlib.util.find_spec("sentence_transformers") is not None
    )


def test_machine_installs_the_real_agents(tmp_path, monkeypatch, stub_bin) -> None:
    # The Machine's two changes, end to end: a plist written where launchd reads
    # it, the agent bootstrapped, and the installed backup's dest readable back
    # off its own plist.
    _darwin(monkeypatch)  # _launchd's own macOS gate, below the Machine
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    log = _launchctl_stub(stub_bin, {"bootout": (1, "", ""), "bootstrap": (0, "", "")})
    arc = str(tmp_path / "arc")

    m = _MacMachine()
    m.install_watcher(arc)
    m.install_backup("/Volumes/B/arc", arc)

    written = plistlib.loads(_launchd._plist_path(WATCHER_LABEL).read_bytes())
    assert written["Label"] == WATCHER_LABEL
    assert written["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == arc
    assert m.backup_dest() == "/Volumes/B/arc"
    assert _subcommands(log) == ["bootout", "bootstrap", "bootout", "bootstrap"]


# ── print_status: the darwin + recorded-backup/verify branches ────────────────


def _seed_threads(tmp_path, conversations: int = 0, topics: int = 0) -> None:
    """Real rows in the real index: imported conversations plus curated topics."""
    from tests.helpers import import_cc_session

    for i in range(conversations):
        import_cc_session(tmp_path, f"conv{i}")
    if topics:
        from thread_archive._store import Thread, get_session

        with get_session() as s:
            for i in range(topics):
                s.add(Thread(name=f"topic-{i}", title=f"Topic {i}", thread_type="topic"))
            s.commit()


def test_print_status_with_backup_and_verify_records(archive_home, tmp_path, capsys) -> None:
    _seed_threads(tmp_path, conversations=1)
    record_health("backup_last", {"ok": True, "at": "2026-07-14T04:00:00+00:00",
                                  "dest": "/Volumes/B/arc"})
    record_health("verify_last", {"ok": False, "at": "2026-07-13T04:00:00+00:00"})
    machine = FakeMachine(watcher=True, backup=True, backup_dest="/Volumes/B/arc")

    assert wizard.print_status(_args("status"), machine=machine) == 0
    out = capsys.readouterr().out
    assert "1 conversations" in out
    assert "watcher:  running" in out
    assert "backup:   ok" in out and "/Volumes/B/arc" in out
    assert "verify:   FAILED" in out
    assert "schedule: nightly backup" in out


def test_print_status_red_nightly_and_topic_split(archive_home, tmp_path, capsys) -> None:
    # The scheduled pipeline's verdict must be visible next to a green ad-hoc
    # backup, and topic threads must not be counted as conversations.
    _seed_threads(tmp_path, conversations=2, topics=3)
    record_health("backup_last", {"ok": True, "at": "2026-07-14T04:00:00+00:00",
                                  "dest": "/Volumes/B/arc"})
    record_health("nightly_last", {"ok": False, "at": "2026-07-15T04:00:00+00:00",
                                   "dest": "/Volumes/B/arc",
                                   "failed_stages": ["backup", "restore-drill"]})
    machine = FakeMachine(watcher=True, backup=True, backup_dest="/Volumes/B/arc")

    assert wizard.print_status(_args("status"), machine=machine) == 0
    out = capsys.readouterr().out
    assert "2 conversations · 3 topics" in out
    assert "backup:   ok" in out
    assert "nightly:  FAILED (backup, restore-drill)" in out


def test_print_status_curation_lines(archive_home, capsys) -> None:
    assert wizard.print_status(
        _args("status"), machine=FakeMachine(curation=True)
    ) == 0
    assert "curation: librarian (hourly) + gardener (daily) scheduled" in capsys.readouterr().out

    assert wizard.print_status(
        _args("status"), machine=FakeMachine(curation_installed=True)
    ) == 0
    assert "curation: not scheduled" in capsys.readouterr().out


# ── _setup_completed ──────────────────────────────────────────────────────────


def test_setup_completed_true_from_populated_archive(archive_home, tmp_path) -> None:
    # No config at all, but a populated index → treat as already set up.
    from tests.helpers import import_cc_session

    import_cc_session(tmp_path)
    assert wizard._setup_completed(_args()) is True


def test_setup_completed_false_when_status_raises(archive_home) -> None:
    # Index file exists but is not a database → an unreadable home reads as fresh.
    from thread_archive._config import resolve_paths

    idx = resolve_paths(str(archive_home)).index_path
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(b"not a db")

    assert wizard._setup_completed(_args("--home", str(archive_home))) is False


def test_main_setup_command_dispatches_to_run_setup(archive_home, capsys) -> None:
    # main(["setup"]) with no TTY and no --yes lands on the guidance path of
    # run_setup — exercises the explicit `setup` dispatch without scanning.
    assert wizard.main(["setup"]) == 0
    assert "Nothing was imported" in capsys.readouterr().out
