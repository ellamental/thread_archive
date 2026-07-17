"""Branch-coverage tests for the setup/install machinery: the ``_launchd``
LaunchAgent builder + load/unload/restart/status wrappers, the ``_setup.clients``
MCP-client detection/wiring, and the untouched branches of the ``_setup.wizard``
first-run flow.

Nothing here touches real launchctl, the real ``~/Library/LaunchAgents``, or a
real ``claude`` CLI: the subprocess seam is faked (so the real ``_launchctl`` /
``_run`` bodies still run), ``$HOME`` is redirected per test so plist writes land
in ``tmp_path``, and the wizard's watcher/backup/mcp offers are driven through
hand-written stubs. Complements ``test_launchd.py`` (pure-dict plist shape) and
``test_setup_wizard.py`` (config persistence + the non-TTY safety stance).
"""

from __future__ import annotations

import builtins
import contextlib
import plistlib
import subprocess
import time
from pathlib import Path

import pytest

from thread_archive import _api, _launchd
from thread_archive._launchd import BACKUP_LABEL, MCP_LABEL, WATCHER_LABEL
from thread_archive._setup import clients, wizard
from thread_archive._watcher import lazy
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult

# ── fakes ────────────────────────────────────────────────────────────────────


class FakeLaunchctl:
    """Stands in for ``_launchd.subprocess.run`` — records every ``launchctl``
    invocation and returns a scripted result keyed by the subcommand, leaving
    the real ``_launchctl`` / ``_uid`` bodies to execute."""

    def __init__(self, results: dict | None = None):
        self.calls: list[list[str]] = []
        # subcommand -> (returncode, stdout, stderr)
        self.results = results or {}

    def __call__(self, argv, *, check=False, capture_output=False, text=False, **kw):
        self.calls.append(list(argv))
        sub = argv[1] if len(argv) > 1 else ""
        rc, out, err = self.results.get(sub, (0, "", ""))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    @property
    def subcommands(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) > 1]


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


def _force_darwin(monkeypatch, module=wizard) -> None:
    monkeypatch.setattr(module.sys, "platform", "darwin")


def _bindir(tmp_path, monkeypatch) -> Path:
    """A fake console-script bin dir whose ``python`` sibling carries the
    ``archive`` / ``archive-mcp`` scripts, so ``_entry_path`` resolves them via
    the ``is_file()`` branch without depending on the real install layout."""
    b = tmp_path / "bin"
    b.mkdir(parents=True, exist_ok=True)
    for n in ("python", "archive", "archive-mcp"):
        (b / n).write_text("#!/bin/sh\n")
    monkeypatch.setattr(_launchd.sys, "executable", str(b / "python"))
    return b


# ══════════════════════════════════════════════════════════════════════════════
# _launchd
# ══════════════════════════════════════════════════════════════════════════════


def test_require_darwin_gate(monkeypatch) -> None:
    # Forced on-mac the gate is a no-op; forced off-mac it aborts (next test).
    monkeypatch.setattr(_launchd.sys, "platform", "darwin")
    _launchd._require_darwin()  # does not raise on darwin


def test_require_darwin_raises_off_mac(monkeypatch) -> None:
    monkeypatch.setattr(_launchd.sys, "platform", "linux")
    with pytest.raises(SystemExit, match="macOS-only"):
        _launchd._require_darwin()


def test_entry_path_next_to_executable(tmp_path, monkeypatch) -> None:
    b = _bindir(tmp_path, monkeypatch)
    assert _launchd._entry_path("archive") == b / "archive"


def test_entry_path_falls_back_to_which(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_launchd.sys, "executable", str(tmp_path / "nopy"))
    monkeypatch.setattr(_launchd.shutil, "which", lambda n: f"/usr/local/bin/{n}")
    assert _launchd._entry_path("archive") == Path("/usr/local/bin/archive")


def test_entry_path_missing_script_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_launchd.sys, "executable", str(tmp_path / "nopy"))
    monkeypatch.setattr(_launchd.shutil, "which", lambda n: None)
    with pytest.raises(SystemExit, match="cannot find the `archive` console script"):
        _launchd._entry_path("archive")


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


def test_install_watcher_writes_plist_and_loads(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # Path.home() → tmp plist dir
    _bindir(tmp_path, monkeypatch)
    fake = FakeLaunchctl(results={"bootout": (0, "", ""), "bootstrap": (0, "", "")})
    monkeypatch.setattr(_launchd.subprocess, "run", fake)
    slept: list[float] = []
    monkeypatch.setattr(_launchd.time, "sleep", lambda s: slept.append(s))

    arc = str(tmp_path / "arc")
    plist_path = _launchd.install_watcher(arc)

    assert plist_path == _launchd._plist_path(WATCHER_LABEL)
    assert plist_path.exists()
    written = plistlib.loads(plist_path.read_bytes())
    assert written["Label"] == WATCHER_LABEL
    assert written["ProgramArguments"][1] == "watch"
    assert "--web" in written["ProgramArguments"]
    assert written["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == arc
    # bootout (existing agent) → settle → bootstrap, in that order.
    assert fake.subcommands == ["bootout", "bootstrap"]
    assert slept == [3]
    assert (Path(arc) / "logs").is_dir()  # log dir created for StandardOutPath


def test_install_mcp_bootstrap_failure_raises_and_skips_settle(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _bindir(tmp_path, monkeypatch)
    # bootout returns nonzero (nothing was loaded) → no settle sleep; bootstrap fails.
    fake = FakeLaunchctl(results={"bootout": (1, "", ""), "bootstrap": (5, "", "Bootstrap failed: 5")})
    monkeypatch.setattr(_launchd.subprocess, "run", fake)
    slept: list[float] = []
    monkeypatch.setattr(_launchd.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(SystemExit, match="bootstrap failed: Bootstrap failed: 5"):
        _launchd.install_mcp(str(tmp_path / "arc"))
    assert slept == []  # bootout nonzero → skipped the settle


def test_install_backup_writes_schedule(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _bindir(tmp_path, monkeypatch)
    monkeypatch.setattr(_launchd.subprocess, "run",
                        FakeLaunchctl(results={"bootout": (1, "", ""), "bootstrap": (0, "", "")}))
    monkeypatch.setattr(_launchd.time, "sleep", lambda s: None)

    plist_path = _launchd.install_backup("/Volumes/Backup/arc", str(tmp_path / "arc"),
                                         hour=2, minute=15)
    written = plistlib.loads(plist_path.read_bytes())
    assert written["Label"] == BACKUP_LABEL
    assert written["ProgramArguments"][-2:] == ["nightly", "/Volumes/Backup/arc"]
    assert written["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}


def test_uninstall_agent_boots_out_and_removes_plist(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake = FakeLaunchctl()
    monkeypatch.setattr(_launchd.subprocess, "run", fake)
    plist = _launchd._plist_path(WATCHER_LABEL)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"stale")

    _launchd.uninstall_watcher()
    assert not plist.exists()
    assert fake.subcommands == ["bootout"]
    assert fake.calls[0][2] == f"gui/{_launchd._uid()}/{WATCHER_LABEL}"


def test_uninstall_agent_missing_plist_is_ok(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(_launchd.subprocess, "run", FakeLaunchctl())
    _launchd.uninstall_mcp()  # unlink(missing_ok=True): no such plist, no raise


def test_restart_agent_kickstarts(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    fake = FakeLaunchctl(results={"kickstart": (0, "", "")})
    monkeypatch.setattr(_launchd.subprocess, "run", fake)
    _launchd.restart_mcp()
    assert fake.subcommands == ["kickstart"]
    assert "-k" in fake.calls[0]
    assert fake.calls[0][-1] == f"gui/{_launchd._uid()}/{MCP_LABEL}"


def test_restart_agent_failure_raises(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setattr(_launchd.subprocess, "run",
                        FakeLaunchctl(results={"kickstart": (1, "", "No such process")}))
    with pytest.raises(SystemExit, match="kickstart failed: No such process"):
        _launchd.restart_backup()


def test_remaining_wrappers_delegate(tmp_path, monkeypatch) -> None:
    # The watcher/backup twins of the wrappers exercised above route through the
    # same helpers; pin them so the whole delegation set is covered.
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake = FakeLaunchctl(results={"kickstart": (0, "", ""), "print": (1, "", "")})
    monkeypatch.setattr(_launchd.subprocess, "run", fake)
    _launchd.restart_watcher()
    _launchd.uninstall_backup()
    assert _launchd.backup_status() == f"{BACKUP_LABEL}: not loaded"
    assert "kickstart" in fake.subcommands
    assert "bootout" in fake.subcommands
    assert "print" in fake.subcommands


def test_agent_status_not_loaded(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setattr(_launchd.subprocess, "run",
                        FakeLaunchctl(results={"print": (1, "", "")}))
    assert _launchd.watcher_status() == f"{WATCHER_LABEL}: not loaded"


def test_agent_status_parses_fields(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    stdout = (
        f"{MCP_LABEL} = {{\n"
        "\tstate = running\n"
        "\tpid = 4242\n"
        "\tprogram = /env/bin/archive-mcp\n"
        "\tactive count = 1\n"  # dropped: not one of the kept keys
        "}\n"
    )
    monkeypatch.setattr(_launchd.subprocess, "run",
                        FakeLaunchctl(results={"print": (0, stdout, "")}))
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
    _force_darwin(monkeypatch, _launchd)
    _write_backup_plist(tmp_path, monkeypatch,
                        ["/env/bin/archive", "nightly", "/Volumes/B/arc"])
    assert _launchd.backup_agent_dest() == "/Volumes/B/arc"


def test_backup_agent_dest_none_without_nightly(tmp_path, monkeypatch) -> None:
    # An external wrapper-script shape: no bare `nightly` arg → unknown dest.
    _force_darwin(monkeypatch, _launchd)
    _write_backup_plist(tmp_path, monkeypatch, ["/somewhere/run-nightly.sh"])
    assert _launchd.backup_agent_dest() is None


def test_backup_agent_dest_none_when_plist_absent(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch, _launchd)
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    assert _launchd.backup_agent_dest() is None


def test_backup_agent_dest_none_off_mac(monkeypatch) -> None:
    monkeypatch.setattr(_launchd.sys, "platform", "linux")
    assert _launchd.backup_agent_dest() is None


# ══════════════════════════════════════════════════════════════════════════════
# _setup.clients
# ══════════════════════════════════════════════════════════════════════════════


def test_console_script_next_to_executable(tmp_path, monkeypatch) -> None:
    b = tmp_path / "bin"
    b.mkdir()
    (b / "python").write_text("x")
    (b / "archive-mcp").write_text("x")
    monkeypatch.setattr(clients.sys, "executable", str(b / "python"))
    assert clients.console_script("archive-mcp") == str(b / "archive-mcp")


def test_console_script_falls_back_to_which(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(clients.sys, "executable", str(tmp_path / "nopy"))
    monkeypatch.setattr(clients.shutil, "which", lambda n: f"/usr/local/bin/{n}")
    assert clients.console_script("archive-mcp") == "/usr/local/bin/archive-mcp"


def test_console_script_bare_name_last_resort(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(clients.sys, "executable", str(tmp_path / "nopy"))
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.console_script("archive-mcp") == "archive-mcp"


def test_claude_cli_uses_which(monkeypatch) -> None:
    monkeypatch.setattr(clients.shutil, "which",
                        lambda n: "/usr/bin/claude" if n == "claude" else None)
    assert clients.claude_cli() == "/usr/bin/claude"
    monkeypatch.setattr(clients.shutil, "which", lambda n: None)
    assert clients.claude_cli() is None


def test_claude_has_server_true_and_false(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(clients.subprocess, "run", fake_run)
    assert clients.claude_has_server("/bin/claude") is True
    assert calls[0] == ["/bin/claude", "mcp", "get", "thread-archive"]

    monkeypatch.setattr(clients.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
    assert clients.claude_has_server("/bin/claude") is False


def test_claude_has_server_probe_failure_is_unknown(monkeypatch) -> None:
    def raise_os(argv, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(clients.subprocess, "run", raise_os)
    assert clients.claude_has_server("/bin/claude") is None

    def raise_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, clients._SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(clients.subprocess, "run", raise_timeout)
    assert clients.claude_has_server("/bin/claude") is None


def test_wire_claude_success_adds_both_servers(monkeypatch) -> None:
    monkeypatch.setattr(clients, "console_script", lambda n: f"/env/bin/{n}")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(clients.subprocess, "run", fake_run)
    assert clients.wire_claude("/bin/claude") == []
    assert len(calls) == 2
    assert calls[0][:6] == ["/bin/claude", "mcp", "add", "--scope", "user", "thread-archive"]
    assert calls[0][-1] == "/env/bin/archive-mcp"
    assert calls[1][5] == "thread-archive-librarian"
    assert calls[1][-1] == "/env/bin/archive-librarian-mcp"


def test_wire_claude_reports_subprocess_errors(monkeypatch) -> None:
    monkeypatch.setattr(clients, "console_script", lambda n: f"/env/bin/{n}")

    def raise_os(argv, **kw):
        raise OSError("exec format error")

    monkeypatch.setattr(clients.subprocess, "run", raise_os)
    errors = clients.wire_claude("/bin/claude")
    assert len(errors) == 2
    assert all("exec format error" in e for e in errors)


def test_wire_claude_uses_stderr_detail_and_fallback(monkeypatch) -> None:
    monkeypatch.setattr(clients, "console_script", lambda n: f"/env/bin/{n}")

    def fake_run(argv, **kw):
        server = argv[5]
        if server == clients.SEARCH_SERVER:
            return subprocess.CompletedProcess(argv, 1, "", "usage: claude mcp\nboom detail")
        return subprocess.CompletedProcess(argv, 1, "", "")  # no detail → generic message

    monkeypatch.setattr(clients.subprocess, "run", fake_run)
    errors = clients.wire_claude("/bin/claude")
    assert errors[0] == "thread-archive: boom detail"
    assert errors[1] == "thread-archive-librarian: claude mcp add failed"


# ══════════════════════════════════════════════════════════════════════════════
# _setup.wizard — formatting + prompt helpers
# ══════════════════════════════════════════════════════════════════════════════


def test_fmt_when_yesterday_and_old() -> None:
    now = time.time()
    assert wizard._fmt_when(now - 30 * 3600) == "yesterday"  # 24–48h band
    old = time.mktime(time.strptime("2020-06-15", "%Y-%m-%d"))
    assert wizard._fmt_when(old) == "Jun 2020"  # >48h → month/year


def test_ask_interactive_reads_lowers_and_defaults(monkeypatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda prompt: "  YeS ")
    assert wizard._ask("q> ", default="d", interactive=True) == "yes"
    monkeypatch.setattr(builtins, "input", lambda prompt: "")
    assert wizard._ask("q> ", default="d", interactive=True) == "d"

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)
    assert wizard._ask("q> ", default="d", interactive=True) == "d"
    # Non-interactive never reads input at all.
    assert wizard._ask("q> ", default="d", interactive=False) == "d"


def test_ask_path_preserves_case_and_handles_eof(monkeypatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda prompt: "  /Some/Mixed/Path ")
    assert wizard._ask_path("q> ", interactive=True) == "/Some/Mixed/Path"

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)
    assert wizard._ask_path("q> ", interactive=True) == ""
    assert wizard._ask_path("q> ", interactive=False) == ""


# ── run_setup: discover + import branches ─────────────────────────────────────


def test_discover_exception_marks_source_absent(archive_home, capsys) -> None:
    good = FakeWatcher("claude-code")
    broken = FakeWatcher("cursor", discover_raises=True)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp", "--skip-curation"),
        watchers=[good, broken],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "not found: Cursor" in out  # a discover() that raised reads as absent
    assert good.polled == 1


def test_import_skipped_when_owner_lock_unavailable(archive_home, monkeypatch, capsys) -> None:
    @contextlib.contextmanager
    def not_owned():
        yield False

    monkeypatch.setattr(lazy, "try_ingest_owner_lock", not_owned)
    fake = FakeWatcher("claude-code")
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp", "--skip-curation"),
        watchers=[fake],
    )
    assert rc == 0
    assert fake.polled == 0  # another watcher owns ingest → manual import stands down
    assert "already ingests for this archive" in capsys.readouterr().out


def test_import_poll_failure_is_reported(archive_home, capsys) -> None:
    # A source whose poll raises: the failure is captured, no maintain() runs
    # (zero events), and the error summary is printed.
    boom = FakeWatcher("claude-code", poll_raises=True)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-watcher", "--skip-backup", "--skip-mcp", "--skip-curation"),
        watchers=[boom],
    )
    assert rc == 0
    assert boom.polled == 1
    out = capsys.readouterr().out
    assert "failed: poll blew up" in out
    assert "1 item(s) could not be imported" in out


def test_setup_reports_web_viewer_and_missing_embeddings(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wizard, "embeddings_installed", lambda: False)
    rc = wizard.run_setup(
        _args("setup", "--yes", "--skip-import"), watchers=[],
        offer_watcher=lambda args, interactive: "launchd",
        offer_backup=lambda args, interactive: {"status": "skipped"},
        offer_mcp=lambda args, interactive: "skipped",
        offer_curation=lambda args, interactive: "skipped",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "web viewer:       http://127.0.0.1:8787" in out  # watcher == launchd
    assert "semantic search:  not installed" in out


# ── _offer_watcher ────────────────────────────────────────────────────────────


def test_offer_watcher_skip_flag(archive_home, capsys) -> None:
    assert wizard._offer_watcher(_args("setup", "--skip-watcher"), interactive=False) == "skipped"
    assert "Watcher skipped" in capsys.readouterr().out


def test_offer_watcher_non_darwin_unavailable(archive_home, monkeypatch) -> None:
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    assert wizard._offer_watcher(_args("setup"), interactive=False) == "unavailable"


def test_offer_watcher_already_running(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: True)
    assert wizard._offer_watcher(_args("setup"), interactive=False) == "already-running"
    assert "already installed and running" in capsys.readouterr().out


def test_offer_watcher_installs(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: False)
    recorded = {}
    monkeypatch.setattr(_launchd, "install_watcher",
                        lambda home=None, **kw: recorded.setdefault("home", home) or Path("/x"))
    # interactive=False → _ask returns the "" default → install.
    assert wizard._offer_watcher(_args("setup", "--yes"), interactive=False) == "launchd"
    assert "home" in recorded
    assert "Installed" in capsys.readouterr().out


def test_offer_watcher_skip_answer(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: False)
    monkeypatch.setattr(_launchd, "install_watcher",
                        lambda *a, **k: pytest.fail("must not install on skip"))
    assert wizard._offer_watcher(
        _args("setup"), interactive=True,
        ask=lambda prompt, *, default, interactive: "s",
    ) == "skipped"
    assert "lazy catch-up covers freshness" in capsys.readouterr().out


def test_offer_watcher_install_failure(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: False)

    def boom(home=None, **kw):
        raise SystemExit("launchctl bootstrap failed")

    monkeypatch.setattr(_launchd, "install_watcher", boom)
    assert wizard._offer_watcher(_args("setup", "--yes"), interactive=False) == "failed"
    assert "Could not install the watcher" in capsys.readouterr().out


# ── _offer_backup: the two untouched branches ─────────────────────────────────


def test_offer_backup_relative_dest_is_resolved(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)
    recorded = {}
    monkeypatch.setattr(_launchd, "install_backup",
                        lambda dest, home=None, **kw: recorded.update(dest=dest))
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "rel/backups"), interactive=False
    )
    assert out["status"] == "launchd"
    assert Path(out["dest"]).is_absolute()  # a relative dest was resolved to absolute
    assert out["dest"].endswith("rel/backups")
    assert recorded["dest"] == out["dest"]


def test_offer_backup_install_failure(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: False)

    def boom(dest, home=None, **kw):
        raise SystemExit("launchctl bootstrap failed")

    monkeypatch.setattr(_launchd, "install_backup", boom)
    out = wizard._offer_backup(
        _args("setup", "--yes", "--backup-dest", "/Volumes/B/arc"), interactive=False
    )
    assert out == {"status": "failed"}
    assert "Could not schedule backup" in capsys.readouterr().out


# ── _offer_mcp ────────────────────────────────────────────────────────────────


def test_offer_mcp_skip_flag(archive_home, capsys) -> None:
    assert wizard._offer_mcp(_args("setup", "--skip-mcp"), interactive=False) == "skipped"
    assert "MCP wiring skipped" in capsys.readouterr().out


def test_offer_mcp_no_client_prints_config(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: None)
    assert wizard._offer_mcp(_args("setup"), interactive=False) == "printed"
    assert "mcpServers" in capsys.readouterr().out


def test_offer_mcp_already_wired(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(clients, "claude_has_server", lambda cli: True)
    assert wizard._offer_mcp(_args("setup"), interactive=False) == "already-wired"
    assert "already has the archive's MCP servers" in capsys.readouterr().out


def test_offer_mcp_print_only_answer(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(clients, "claude_has_server", lambda cli: False)
    monkeypatch.setattr(clients, "wire_claude",
                        lambda cli: pytest.fail("print-only must not wire"))
    assert wizard._offer_mcp(
        _args("setup"), interactive=True,
        ask=lambda prompt, *, default, interactive: "p",
    ) == "printed"
    assert "mcpServers" in capsys.readouterr().out


def test_offer_mcp_skip_answer(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(clients, "claude_has_server", lambda cli: False)
    assert wizard._offer_mcp(
        _args("setup"), interactive=True,
        ask=lambda prompt, *, default, interactive: "s",
    ) == "skipped"
    assert "thread_archive setup" in capsys.readouterr().out


def test_offer_mcp_wires_successfully(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(clients, "claude_has_server", lambda cli: False)
    wired = {}

    def fake_wire(cli):
        wired["cli"] = cli
        return []

    monkeypatch.setattr(clients, "wire_claude", fake_wire)
    assert wizard._offer_mcp(_args("setup", "--yes"), interactive=False) == "wired"
    assert wired["cli"] == "/bin/claude"
    assert "Wired:" in capsys.readouterr().out


def test_offer_mcp_wiring_failure(archive_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(clients, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(clients, "claude_has_server", lambda cli: False)
    monkeypatch.setattr(clients, "wire_claude", lambda cli: ["thread-archive: nope"])
    assert wizard._offer_mcp(_args("setup", "--yes"), interactive=False) == "failed"
    out = capsys.readouterr().out
    assert "Wiring hit trouble" in out
    assert "! thread-archive: nope" in out


# ── _watcher_running / _backup_running ────────────────────────────────────────


def _fake_launchctl(monkeypatch, rc):
    """The probe tests' one-liner: a launchctl whose ``print`` answers ``rc``,
    installed at the subprocess boundary (the real ``_launchctl`` body runs)."""
    monkeypatch.setattr(_launchd.subprocess, "run",
                        FakeLaunchctl(results={"print": (rc, "", "")}))


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
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    assert wizard.watcher_running() is False


def test_watcher_running_not_loaded(monkeypatch) -> None:
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 1)
    assert wizard.watcher_running() is False


def test_watcher_running_matches_this_home(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 0)
    agent_home = str(tmp_path / "agent")
    _install_plist(monkeypatch, tmp_path, WATCHER_LABEL, agent_home)
    assert wizard.watcher_running(agent_home) is True
    assert wizard.watcher_running(str(tmp_path / "elsewhere")) is False


def test_watcher_running_unreadable_plist_assumes_default(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 0)
    monkeypatch.setenv("HOME", str(tmp_path))  # no plist under this home
    assert wizard.watcher_running() is True  # loaded, plist unreadable → default wiring


def test_backup_running_non_darwin(monkeypatch) -> None:
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    assert wizard.backup_running() is False


def test_backup_running_not_loaded(monkeypatch) -> None:
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 1)
    assert wizard.backup_running() is False


def test_backup_running_no_home_env_reads_as_default(tmp_path, monkeypatch) -> None:
    # The host/ install shape: a plist with no home env is read as the default
    # wiring, so it counts as covering the default home.
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 0)
    _install_plist(monkeypatch, tmp_path, BACKUP_LABEL, None)
    assert wizard.backup_running() is True


def test_backup_running_unreadable_plist_assumes_default(tmp_path, monkeypatch) -> None:
    _force_darwin(monkeypatch)
    _fake_launchctl(monkeypatch, 0)
    monkeypatch.setenv("HOME", str(tmp_path))  # no plist under this home
    assert wizard.backup_running() is True


# ── print_status: the darwin + recorded-backup/verify branches ────────────────


def test_print_status_with_backup_and_verify_records(archive_home, monkeypatch, capsys) -> None:
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: True)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: True)
    monkeypatch.setattr(_launchd, "backup_agent_dest", lambda: "/Volumes/B/arc")
    fake_status = {
        "home": str(archive_home), "threads": 3, "events": 12, "fts_indexed": 9,
        "last_backup": {"ok": True, "at": "2026-07-14T04:00:00+00:00", "dest": "/Volumes/B/arc"},
        "last_verify": {"ok": False, "at": "2026-07-13T04:00:00+00:00"},
    }
    monkeypatch.setattr(_api, "status", lambda home=None: fake_status)
    assert wizard.print_status(_args("status")) == 0
    out = capsys.readouterr().out
    assert "watcher:  running" in out
    assert "backup:   ok" in out and "/Volumes/B/arc" in out
    assert "verify:   FAILED" in out
    assert "schedule: nightly backup" in out


def test_print_status_red_nightly_and_topic_split(archive_home, monkeypatch, capsys) -> None:
    # The scheduled pipeline's verdict must be visible next to a green ad-hoc
    # backup, and topic threads must not be counted as conversations.
    _force_darwin(monkeypatch)
    monkeypatch.setattr(wizard, "watcher_running", lambda home=None: True)
    monkeypatch.setattr(wizard, "backup_running", lambda home=None: True)
    monkeypatch.setattr(_launchd, "backup_agent_dest", lambda: "/Volumes/B/arc")
    fake_status = {
        "home": str(archive_home), "threads": 10, "topics": 4, "events": 12, "fts_indexed": 9,
        "last_backup": {"ok": True, "at": "2026-07-14T04:00:00+00:00", "dest": "/Volumes/B/arc"},
        "last_nightly": {"ok": False, "at": "2026-07-15T04:00:00+00:00",
                         "dest": "/Volumes/B/arc", "failed_stages": ["backup", "restore-drill"]},
    }
    monkeypatch.setattr(_api, "status", lambda home=None: fake_status)
    assert wizard.print_status(_args("status")) == 0
    out = capsys.readouterr().out
    assert "6 conversations · 4 topics" in out
    assert "backup:   ok" in out
    assert "nightly:  FAILED (backup, restore-drill)" in out


# ── _setup_completed ──────────────────────────────────────────────────────────


def test_setup_completed_true_from_populated_archive(archive_home, tmp_path, monkeypatch) -> None:
    # No config at all, but a populated index → treat as already set up.
    from tests.helpers import import_cc_session

    import_cc_session(tmp_path)
    assert wizard._setup_completed(_args()) is True


def test_setup_completed_false_when_status_raises(archive_home, monkeypatch) -> None:
    # Index file exists but status blows up (unreadable home) → reads as fresh.
    from thread_archive._config import resolve_paths

    idx = resolve_paths(str(archive_home)).index_path
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(b"not a db")

    def boom(home=None):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(_api, "status", boom)
    assert wizard._setup_completed(_args("--home", str(archive_home))) is False


def test_main_setup_command_dispatches_to_run_setup(archive_home, capsys) -> None:
    # main(["setup"]) with no TTY and no --yes lands on the guidance path of
    # run_setup — exercises the explicit `setup` dispatch without scanning.
    assert wizard.main(["setup"]) == 0
    assert "Nothing was imported" in capsys.readouterr().out
