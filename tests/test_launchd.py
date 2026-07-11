"""The package-generated watcher LaunchAgent plist (``_launchd``).

Pure-dict tests only — nothing here touches launchctl or the real
``~/Library/LaunchAgents``; the install/uninstall/restart wrappers are thin
subprocess shells around it.
"""

from __future__ import annotations

from pathlib import Path

from thread_archive._launchd import WATCHER_LABEL, watcher_plist

ENTRY = Path("/opt/venv/bin/archive")
LOG_DIR = Path("/data/arc/logs")


def test_watcher_plist_shape() -> None:
    p = watcher_plist(ENTRY, LOG_DIR)
    assert p["Label"] == WATCHER_LABEL
    assert p["ProgramArguments"][:2] == [str(ENTRY), "watch"]
    assert "--web" in p["ProgramArguments"]  # viewer cohosted by default
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"Crashed": True}
    assert p["LimitLoadToSessionType"] == "Aqua"
    assert p["StandardOutPath"] == str(LOG_DIR / "watcher-stdout.log")
    assert p["StandardErrorPath"] == str(LOG_DIR / "watcher-stderr.log")
    # launchd doesn't inherit a venv: the entry's own bin dir must lead PATH.
    assert p["EnvironmentVariables"]["PATH"].startswith(f"{ENTRY.parent}:")


def test_watcher_plist_home_and_web_options() -> None:
    p = watcher_plist(ENTRY, LOG_DIR, home="/data/arc", web=False)
    assert p["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert "--web" not in p["ProgramArguments"]

    default = watcher_plist(ENTRY, LOG_DIR)
    # No explicit home → the agent resolves the default; the env var stays unset.
    assert "THREAD_ARCHIVE_HOME" not in default["EnvironmentVariables"]
    custom_port = watcher_plist(ENTRY, LOG_DIR, web_port=9000)
    assert "9000" in custom_port["ProgramArguments"]
