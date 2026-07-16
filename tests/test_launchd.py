"""The package-generated watcher LaunchAgent plist (``_launchd``).

Pure-dict tests only — nothing here touches launchctl or the real
``~/Library/LaunchAgents``; the install/uninstall/restart wrappers are thin
subprocess shells around it.
"""

from __future__ import annotations

from pathlib import Path

from thread_archive._launchd import (
    BACKUP_LABEL,
    GARDENER_LABEL,
    LIBRARIAN_LABEL,
    WATCHER_LABEL,
    backup_plist,
    gardener_plist,
    librarian_plist,
    resolved_gardener_schedule,
    resolved_librarian_interval,
    watcher_plist,
)

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


def test_backup_plist_shape() -> None:
    p = backup_plist(ENTRY, LOG_DIR, "/Volumes/Backup/arc")
    assert p["Label"] == BACKUP_LABEL
    assert p["ProgramArguments"] == [str(ENTRY), "nightly", "/Volumes/Backup/arc"]
    # A scheduled one-shot, not a resident agent: fire daily, don't RunAtLoad.
    assert p["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert p["RunAtLoad"] is False
    assert "KeepAlive" not in p
    assert p["ProcessType"] == "Background"  # nice'd, I/O-bound
    assert p["StandardOutPath"] == str(LOG_DIR / "backup-stdout.log")
    assert p["EnvironmentVariables"]["PATH"].startswith(f"{ENTRY.parent}:")


def test_librarian_plist_shape() -> None:
    p = librarian_plist(ENTRY, LOG_DIR)
    assert p["Label"] == LIBRARIAN_LABEL
    assert p["ProgramArguments"] == [str(ENTRY), "curate", "librarian"]
    # A scheduled one-shot on an hourly interval, not a resident agent.
    assert p["StartInterval"] == 3600
    assert p["RunAtLoad"] is False
    assert "KeepAlive" not in p
    assert p["ProcessType"] == "Background"
    assert p["StandardOutPath"] == str(LOG_DIR / "librarian-stdout.log")
    assert p["EnvironmentVariables"]["PATH"].startswith(f"{ENTRY.parent}:")
    assert "THREAD_ARCHIVE_HOME" not in p["EnvironmentVariables"]
    with_home = librarian_plist(ENTRY, LOG_DIR, home="/data/arc", interval=7200)
    assert with_home["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert with_home["StartInterval"] == 7200


def test_gardener_plist_shape() -> None:
    p = gardener_plist(ENTRY, LOG_DIR)
    assert p["Label"] == GARDENER_LABEL
    assert p["ProgramArguments"] == [str(ENTRY), "curate", "gardener"]
    # Daily, offset from the 04:00 backup.
    assert p["StartCalendarInterval"] == {"Hour": 5, "Minute": 0}
    assert p["RunAtLoad"] is False
    assert "KeepAlive" not in p
    assert p["ProcessType"] == "Background"
    assert p["StandardOutPath"] == str(LOG_DIR / "gardener-stdout.log")
    custom = gardener_plist(ENTRY, LOG_DIR, home="/data/arc", hour=2, minute=15)
    assert custom["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}
    assert custom["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"


def test_resolved_cadence_prefers_config(tmp_path) -> None:
    # A home with no config: the install-time defaults (hourly / daily 05:00).
    from thread_archive._config import save_config

    home = str(tmp_path / "arc")
    assert resolved_librarian_interval(home) == 3600
    assert resolved_gardener_schedule(home) == (5, 0)
    save_config({"curation": {
        "librarian": {"interval_minutes": 120},
        "gardener": {"at": "03:30"},
    }}, home)
    assert resolved_librarian_interval(home) == 7200
    assert resolved_gardener_schedule(home) == (3, 30)


def test_backup_plist_options() -> None:
    p = backup_plist(
        ENTRY, LOG_DIR, "/Volumes/Backup/arc",
        home="/data/arc", hour=2, minute=30, notify_url="http://127.0.0.1:8002/api/notify",
    )
    assert p["EnvironmentVariables"]["THREAD_ARCHIVE_HOME"] == "/data/arc"
    assert p["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert p["ProgramArguments"][-2:] == ["--notify-url", "http://127.0.0.1:8002/api/notify"]
