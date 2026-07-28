"""The package-generated systemd user units (``_service.systemd``).

Two layers, neither of which needs a real init system:

* pure-render tests over the ``*_units`` builders — the systemd analog of
  ``test_launchd.py``'s pure-dict plist tests;
* lifecycle tests over the install/uninstall/restart wrappers, driven against a
  ``systemctl`` / ``loginctl`` stand-in that is the only executable on ``$PATH``,
  so the assertions are the real argv ``systemctl`` received.

A *real* ``systemctl --user`` bootstrap is out of scope here (no user manager on
the CI box or in the slim install container); ``test_systemd_integration.py``
covers that behind an explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thread_archive._service import systemd

ENTRY = Path("/opt/venv/bin/thread-archive")
ENTRY_MCP = Path("/opt/venv/bin/archive-mcp")
LOG_DIR = Path("/data/arc/logs")


# ── stand-in executables ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def stub_bin(tmp_path, monkeypatch) -> Path:
    b = tmp_path / "stub-bin"
    b.mkdir()
    monkeypatch.setenv("PATH", str(b))
    return b


def _stub(stub_bin: Path, name: str, results=None) -> Path:
    """A ``name`` stand-in on PATH, scripted per subcommand
    (``{keyword: returncode}`` matched anywhere in the args; anything unlisted
    exits 0). Returns the argv log it appends to."""
    log = stub_bin.parent / f"{name}.log"
    arms = "\n".join(f'  *{k}*) exit {rc} ;;' for k, rc in (results or {}).items())
    script = "\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        'case "$*" in',
        arms,
        "  *) exit 0 ;;",
        "esac",
    ]) + "\n"
    p = stub_bin / name
    p.write_text(script, encoding="utf-8")
    p.chmod(0o755)
    return log


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [ln.split() for ln in log.read_text(encoding="utf-8").splitlines() if ln]


# ── pure render: watcher ──────────────────────────────────────────────────────


def test_watcher_unit_shape() -> None:
    units = systemd.watcher_units(ENTRY, LOG_DIR)
    assert set(units) == {"thread-archive-watcher.service"}
    text = units["thread-archive-watcher.service"]
    assert "Type=simple" in text
    assert "ExecStart=/opt/venv/bin/thread-archive watch --web --web-port 8787" in text
    # Restart-on-abnormal-exit, and never latch into a permanent 'failed'.
    assert "Restart=on-failure" in text
    assert "StartLimitIntervalSec=0" in text
    assert "RestartSec=5" in text
    assert "LimitNOFILE=4096" in text
    assert "StandardOutput=append:/data/arc/logs/watcher-stdout.log" in text
    assert "StandardError=append:/data/arc/logs/watcher-stderr.log" in text
    assert "WantedBy=default.target" in text
    # The unit inherits no venv: the entry's own bin dir must lead PATH, and no
    # Homebrew leaks into a Linux unit.
    assert "Environment=PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" in text
    assert "homebrew" not in text


def test_watcher_unit_home_and_web_options() -> None:
    text = systemd.watcher_units(ENTRY, LOG_DIR, home="/data/arc", web=False)[
        "thread-archive-watcher.service"
    ]
    assert "Environment=THREAD_ARCHIVE_HOME=/data/arc" in text
    assert "--web" not in text

    default = systemd.watcher_units(ENTRY, LOG_DIR)["thread-archive-watcher.service"]
    assert "THREAD_ARCHIVE_HOME" not in default  # no explicit home → unset

    custom_port = systemd.watcher_units(ENTRY, LOG_DIR, web_port=9000)[
        "thread-archive-watcher.service"
    ]
    assert "--web-port 9000" in custom_port


# ── pure render: mcp ──────────────────────────────────────────────────────────


def test_mcp_unit_shape() -> None:
    text = systemd.mcp_units(ENTRY_MCP, LOG_DIR, host="0.0.0.0", port=9999, ingest=True)[
        "thread-archive-mcp.service"
    ]
    assert "ExecStart=/opt/venv/bin/archive-mcp --http --host 0.0.0.0 --port 9999" in text
    # Every client depends on it: restart on any exit, not only a crash.
    assert "Restart=always" in text
    assert "Environment=THREAD_ARCHIVE_MCP_INGEST=1" in text

    default = systemd.mcp_units(ENTRY_MCP, LOG_DIR)["thread-archive-mcp.service"]
    assert "Environment=THREAD_ARCHIVE_MCP_INGEST=0" in default
    assert "--host 127.0.0.1 --port 8788" in default
    assert "THREAD_ARCHIVE_HOME" not in default


# ── pure render: backup service + timer ───────────────────────────────────────


def test_backup_units_shape() -> None:
    units = systemd.backup_units(ENTRY, LOG_DIR, "/vol/bak")
    assert set(units) == {"thread-archive-backup.service", "thread-archive-backup.timer"}
    svc = units["thread-archive-backup.service"]
    assert "Type=oneshot" in svc
    assert "ExecStart=/opt/venv/bin/thread-archive backup nightly /vol/bak" in svc
    # I/O-bound and not latency-sensitive: nice'd and idle I/O.
    assert "Nice=10" in svc
    assert "IOSchedulingClass=idle" in svc
    # A scheduled one-shot is timer-activated — no [Install], no Restart.
    assert "[Install]" not in svc
    assert "Restart=" not in svc

    timer = units["thread-archive-backup.timer"]
    assert "OnCalendar=*-*-* 04:00:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_backup_units_options() -> None:
    units = systemd.backup_units(
        ENTRY, LOG_DIR, "/vol/bak", home="/data/arc", hour=2, minute=30,
        notify_url="http://127.0.0.1:8002/api/notify",
    )
    svc = units["thread-archive-backup.service"]
    assert "ExecStart=/opt/venv/bin/thread-archive backup nightly /vol/bak --notify-url http://127.0.0.1:8002/api/notify" in svc
    assert "Environment=THREAD_ARCHIVE_HOME=/data/arc" in svc
    assert "OnCalendar=*-*-* 02:30:00" in units["thread-archive-backup.timer"]


# ── read-back probes (parse the on-disk unit) ─────────────────────────────────


def _write_unit(monkeypatch, home: Path, name: str, text: str) -> None:
    monkeypatch.setenv("HOME", str(home))
    d = home / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_agent_home_reads_the_unit(tmp_path, monkeypatch) -> None:
    text = systemd.watcher_units(ENTRY, LOG_DIR, home="/data/arc")[
        "thread-archive-watcher.service"
    ]
    _write_unit(monkeypatch, tmp_path, "thread-archive-watcher.service", text)
    assert systemd._agent_home("watcher") == "/data/arc"


def test_agent_home_none_without_env(tmp_path, monkeypatch) -> None:
    text = systemd.watcher_units(ENTRY, LOG_DIR)["thread-archive-watcher.service"]
    _write_unit(monkeypatch, tmp_path, "thread-archive-watcher.service", text)
    assert systemd._agent_home("watcher") is None


def test_backup_agent_dest_reads_the_unit(tmp_path, monkeypatch) -> None:
    units = systemd.backup_units(ENTRY, LOG_DIR, "/vol/bak", notify_url="http://n")
    _write_unit(monkeypatch, tmp_path, "thread-archive-backup.service", units["thread-archive-backup.service"])
    assert systemd.backup_agent_dest() == "/vol/bak"


def test_backup_agent_dest_none_when_not_installed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # no backup unit written
    assert systemd.backup_agent_dest() is None


# ── lifecycle: the systemctl argv the wrappers actually issue ─────────────────


def test_install_watcher_argv(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sysctl = _stub(stub_bin, "systemctl")
    linger = _stub(stub_bin, "loginctl")

    path = systemd._install(
        systemd.watcher_spec(ENTRY, tmp_path / "logs", home=str(tmp_path))
    )

    # The unit file was written where systemctl --user reads it.
    unit = tmp_path / ".config" / "systemd" / "user" / "thread-archive-watcher.service"
    assert unit.exists()
    assert path == unit
    # linger first (survive logout), then reload → enable → restart the service.
    assert _calls(linger) == [["enable-linger"]]
    assert _calls(sysctl) == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "thread-archive-watcher.service"],
        ["--user", "restart", "thread-archive-watcher.service"],
    ]


def test_install_backup_enables_the_timer_not_the_service(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sysctl = _stub(stub_bin, "systemctl")
    _stub(stub_bin, "loginctl")

    systemd._install(systemd.backup_spec(ENTRY, tmp_path / "logs", "/vol/bak"))

    d = tmp_path / ".config" / "systemd" / "user"
    assert (d / "thread-archive-backup.service").exists()
    assert (d / "thread-archive-backup.timer").exists()
    # enable/restart target the TIMER (arming it), never the oneshot service.
    assert _calls(sysctl) == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "thread-archive-backup.timer"],
        ["--user", "restart", "thread-archive-backup.timer"],
    ]


def test_install_reports_a_restart_failure(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _stub(stub_bin, "systemctl", {"restart": 1})
    _stub(stub_bin, "loginctl")
    with pytest.raises(SystemExit, match="systemctl restart .* failed"):
        systemd._install(systemd.watcher_spec(ENTRY, tmp_path / "logs"))


def test_install_hints_when_linger_refused(tmp_path, monkeypatch, stub_bin, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _stub(stub_bin, "systemctl")
    _stub(stub_bin, "loginctl", {"enable-linger": 1})  # a locked-down box refuses
    systemd._install(systemd.watcher_spec(ENTRY, tmp_path / "logs"))
    # The install proceeds; the operator is told how to make it survive logout.
    assert "enable-linger" in capsys.readouterr().err


def test_uninstall_disables_and_removes(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / "thread-archive-backup.service").write_text("x", encoding="utf-8")
    (d / "thread-archive-backup.timer").write_text("x", encoding="utf-8")
    sysctl = _stub(stub_bin, "systemctl")

    systemd._uninstall("backup")

    assert not (d / "thread-archive-backup.service").exists()
    assert not (d / "thread-archive-backup.timer").exists()
    calls = _calls(sysctl)
    assert ["--user", "disable", "--now", "thread-archive-backup.timer"] in calls
    assert ["--user", "daemon-reload"] in calls


def test_status_not_loaded(tmp_path, monkeypatch, stub_bin) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # `systemctl show` on a missing unit reports LoadState=not-found (exit 0).
    log = stub_bin.parent / "systemctl.log"
    script = "\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        'case "$2" in',
        '  show) echo "LoadState=not-found"; echo "ActiveState=inactive" ;;',
        "esac",
        "exit 0",
    ]) + "\n"
    (stub_bin / "systemctl").write_text(script, encoding="utf-8")
    (stub_bin / "systemctl").chmod(0o755)
    assert "not loaded" in systemd._status("watcher")
