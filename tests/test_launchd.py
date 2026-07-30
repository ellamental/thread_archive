"""The package-generated LaunchAgent plists (``_service.launchd``).

Pure-dict tests only — nothing here touches launchctl or the real
``~/Library/LaunchAgents``; the install/uninstall/restart wrappers are thin
subprocess shells around it.
"""

from __future__ import annotations

from pathlib import Path

from thread_archive._service.launchd import (
    BACKUP_LABEL,
    WATCHER_LABEL,
    backup_plist,
    watcher_plist,
)

ENTRY = Path("/opt/venv/bin/thread-archive")
LOG_DIR = Path("/data/arc/logs")


def test_watcher_plist_shape() -> None:
    # has_viewer pinned: the viewer is dev-only, so the default would make this
    # unit's argv depend on whether the suite runs from a checkout or a wheel.
    p = watcher_plist(ENTRY, LOG_DIR, has_viewer=True)
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
    p = watcher_plist(ENTRY, LOG_DIR, home="/data/arc", web=False, has_viewer=True)
    assert p["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert "--web" not in p["ProgramArguments"]

    default = watcher_plist(ENTRY, LOG_DIR, has_viewer=True)
    # No explicit home → the agent resolves the default; the env var stays unset.
    assert "THREAD_ARCHIVE_HOME" not in default["EnvironmentVariables"]
    custom_port = watcher_plist(ENTRY, LOG_DIR, web_port=9000, has_viewer=True)
    assert "9000" in custom_port["ProgramArguments"]


def test_backup_plist_shape() -> None:
    p = backup_plist(ENTRY, LOG_DIR, "/Volumes/Backup/arc")
    assert p["Label"] == BACKUP_LABEL
    assert p["ProgramArguments"] == [
        str(ENTRY), "backup", "nightly", "/Volumes/Backup/arc"
    ]
    # A scheduled one-shot, not a resident agent: fire daily, don't RunAtLoad.
    assert p["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert p["RunAtLoad"] is False
    assert "KeepAlive" not in p
    assert p["ProcessType"] == "Background"  # nice'd, I/O-bound
    assert p["StandardOutPath"] == str(LOG_DIR / "backup-stdout.log")
    assert p["EnvironmentVariables"]["PATH"].startswith(f"{ENTRY.parent}:")


def test_backup_plist_options() -> None:
    p = backup_plist(
        ENTRY, LOG_DIR, "/Volumes/Backup/arc",
        home="/data/arc", hour=2, minute=30, notify_url="http://127.0.0.1:8002/api/notify",
    )
    assert p["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert p["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert p["ProgramArguments"][-2:] == ["--notify-url", "http://127.0.0.1:8002/api/notify"]
