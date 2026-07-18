"""Branch-coverage tests for the ``archive`` CLI dispatch (:mod:`thread_archive.cli`).

Companion to ``test_cli_smoke.py``: that file covers the happy-path arg mapping of
a handful of verbs; this one drives the still-uncovered verb handlers and their
error / output-formatting branches. Everything heavy (``_api`` calls, the watcher,
launchd, the ingest lock) is stubbed with ``monkeypatch.setattr`` on the module
attribute the handler actually resolves, so no test does real work.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thread_archive import _api as api
from thread_archive import _launchd, _truth, _watcher, _web, cli
from thread_archive._importers import exports
from thread_archive.cli import main

# coverage tag: cli


@contextlib.contextmanager
def _fake_lock():
    """Stand-in for ``shared_ingest_lock`` — a no-op context manager."""
    yield


# ── _parse_hhmm edge cases ────────────────────────────────────────────────────


def test_parse_hhmm_valid() -> None:
    assert cli._parse_hhmm("04:30") == (4, 30)
    assert cli._parse_hhmm("00:00") == (0, 0)
    assert cli._parse_hhmm("23:59") == (23, 59)


def test_parse_hhmm_non_numeric_raises() -> None:
    # int() raises ValueError inside the try → the guarded SystemExit.
    with pytest.raises(SystemExit) as exc:
        cli._parse_hhmm("aa:bb")
    assert "HH:MM" in str(exc.value)


def test_parse_hhmm_missing_colon_raises() -> None:
    # split(":") yields one element → unpack ValueError.
    with pytest.raises(SystemExit):
        cli._parse_hhmm("930")


def test_parse_hhmm_hour_out_of_range_raises() -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parse_hhmm("25:00")
    assert "25:00" in str(exc.value)


def test_parse_hhmm_minute_out_of_range_raises() -> None:
    with pytest.raises(SystemExit):
        cli._parse_hhmm("04:99")


# ── _self_throttle ────────────────────────────────────────────────────────────


def test_self_throttle_runs(monkeypatch) -> None:
    """Drives the real throttle path (nice + on darwin the io-policy syscall);
    nice is stubbed so we don't renice the test runner."""
    import os

    calls = []
    monkeypatch.setattr(os, "nice", lambda n: calls.append(n))
    cli._self_throttle()  # exercises the darwin (or non-darwin) body for real
    assert calls == [10]


def test_self_throttle_non_darwin(monkeypatch) -> None:
    """The non-darwin branch skips the io-policy syscall."""
    import os

    monkeypatch.setattr(os, "nice", lambda n: None)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    cli._self_throttle()  # must not raise; just falls through


# ── _age ──────────────────────────────────────────────────────────────────────


def test_age_invalid_returns_question_mark() -> None:
    assert cli._age("not-a-date") == "?"
    assert cli._age(None) == "?"  # TypeError path


def test_age_naive_and_ranges() -> None:
    # tz-naive input is coerced to UTC; a 3-day-old stamp reads in days.
    assert cli._age("2020-01-01T00:00:00").endswith("d ago")
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert cli._age(recent).endswith("h ago")
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    assert cli._age(old).endswith("d ago")


# ── import ────────────────────────────────────────────────────────────────────


def test_import_line_stream_summary(monkeypatch, capsys) -> None:
    seen = {}

    def fake_import(path, *, home=None, provider=None):
        seen.update(path=path, home=home, provider=provider)
        return SimpleNamespace(thread_id=5, events_created=3, is_new_thread=True)

    monkeypatch.setattr(api, "import_path", fake_import)
    rc = main(["import", "/some/session.jsonl", "--home", "/h"])
    assert rc == 0
    assert seen == {"path": "/some/session.jsonl", "home": "/h", "provider": "claude-code"}
    out = capsys.readouterr().out
    assert "imported /some/session.jsonl (claude-code): thread=5 events=3 new=True" in out


def test_import_db_scanner_summary(monkeypatch, capsys) -> None:
    """The cursor/opencode DB scanners return a result whose ``vars()`` is the
    printed summary (many sessions per file)."""
    monkeypatch.setattr(
        api, "import_path",
        lambda path, **kw: SimpleNamespace(sessions_scanned=2, events_created=10),
    )
    rc = main(["import", "/store.db", "--provider", "cursor", "--home", "/h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported /store.db (cursor):" in out
    assert "sessions_scanned=2" in out and "events_created=10" in out


def test_import_unknown_provider_direct_call() -> None:
    """The registry guard in cmd_import (argparse ``choices`` normally blocks an
    unknown provider before the handler, so exercise the guard directly)."""
    ns = argparse.Namespace(provider="bogus-provider", path="/x", home=None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_import(ns)
    assert "unknown provider 'bogus-provider'" in str(exc.value)


# ── import-export ─────────────────────────────────────────────────────────────


def test_import_export_dispatches(monkeypatch, capsys) -> None:
    opened = {}
    checkpointed = {}
    monkeypatch.setattr(api, "open_archive", lambda home: opened.update(home=home))
    monkeypatch.setattr(api, "checkpoint", lambda *, home=None: checkpointed.update(home=home))
    monkeypatch.setattr(_truth, "shared_ingest_lock", _fake_lock)

    seen = {}

    def fake_export(path, *, force=False):
        seen.update(path=path, force=force)
        return SimpleNamespace(processed=4, imported=3, skipped=1, events_created=42)

    monkeypatch.setattr(exports, "import_export", fake_export)
    rc = main(["import-export", "/export.zip", "--force", "--home", "/h"])
    assert rc == 0
    assert opened == {"home": "/h"} and checkpointed == {"home": "/h"}
    assert seen == {"path": "/export.zip", "force": True}
    out = capsys.readouterr().out
    assert "imported export /export.zip: processed=4 imported=3 skipped=1 events=42" in out


# ── watch ─────────────────────────────────────────────────────────────────────


def _stub_watch_common(monkeypatch):
    monkeypatch.setattr(api, "open_archive", lambda home: None)
    monkeypatch.setattr(_truth, "shared_ingest_lock", _fake_lock)


def test_watch_once_imports_and_maintains(monkeypatch, capsys) -> None:
    _stub_watch_common(monkeypatch)
    maintained = {}

    class FakeWatcher:
        def __init__(self, **kw):
            FakeWatcher.kw = kw

        def poll_once(self):
            return SimpleNamespace(
                sources_checked=2, items_imported=1, events_created=3,
                errors=["cursor: poll error: boom"],
            )

        def maintain(self):
            maintained["yes"] = True
            return {}

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    rc = main(["watch", "--once", "--home", "/h"])
    assert rc == 0
    assert maintained == {"yes": True}  # events_created > 0 triggered upkeep
    # arg mapping through to the Watcher ctor
    assert FakeWatcher.kw["interval"] == 5.0 and FakeWatcher.kw["embed"] is True
    out = capsys.readouterr().out
    assert "watch: checked 2 sources, imported 1 items (3 events)" in out
    assert "! cursor: poll error: boom" in out


def test_watch_once_no_events_skips_maintain(monkeypatch, capsys) -> None:
    _stub_watch_common(monkeypatch)
    maintained = {}

    class FakeWatcher:
        def __init__(self, **kw):
            pass

        def poll_once(self):
            return SimpleNamespace(
                sources_checked=1, items_imported=0, events_created=0, errors=[]
            )

        def maintain(self):  # pragma: no cover - must not be called
            maintained["yes"] = True

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    rc = main(["watch", "--once", "--no-embed"])
    assert rc == 0
    assert maintained == {}
    assert "imported 0 items (0 events)" in capsys.readouterr().out


def test_watch_loop_no_web_returns(monkeypatch) -> None:
    _stub_watch_common(monkeypatch)

    class FakeWatcher:
        maintenance_interval = 300.0

        def __init__(self, **kw):
            pass

        def available(self):
            return [SimpleNamespace(source_name="claude-code")]

        def run(self):
            return None  # returns immediately instead of blocking

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    assert main(["watch"]) == 0


def test_watch_web_cohost_keyboard_interrupt(monkeypatch, capsys) -> None:
    _stub_watch_common(monkeypatch)
    closed = {}

    class FakeHttpd:
        def server_close(self):
            closed["yes"] = True

    served = {}

    def fake_serve(*, host, port):
        served.update(host=host, port=port)
        return FakeHttpd()

    monkeypatch.setattr(_web, "serve_in_thread", fake_serve)

    class FakeWatcher:
        maintenance_interval = 300.0

        def __init__(self, **kw):
            pass

        def available(self):
            return [SimpleNamespace(source_name="cc")]

        def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(_watcher, "Watcher", FakeWatcher)
    rc = main(["watch", "--web", "--web-host", "0.0.0.0", "--web-port", "9999"])
    assert rc == 0
    assert served == {"host": "0.0.0.0", "port": 9999}
    assert closed == {"yes": True}  # finally-clause closed the cohosted server
    assert "stopped." in capsys.readouterr().out


# ── daemon: mcp / backup / watcher lifecycle ─────────────────────────────────


def test_daemon_mcp_lifecycle(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        _launchd, "install_mcp",
        lambda home, *, host, port: seen.update(home=home, host=host, port=port)
        or "/plist/mcp.plist",
    )
    rc = main(["daemon", "install", "--mcp", "--http-host", "1.2.3.4",
               "--http-port", "9", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "host": "1.2.3.4", "port": 9}
    out = capsys.readouterr().out
    assert "shared MCP server: http://1.2.3.4:9/mcp" in out

    monkeypatch.setattr(_launchd, "uninstall_mcp", lambda: None)
    assert main(["daemon", "uninstall", "--mcp"]) == 0
    assert "uninstalled" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "restart_mcp", lambda: None)
    assert main(["daemon", "restart", "--mcp"]) == 0
    assert "restarted" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "mcp_status", lambda: "mcp: loaded")
    assert main(["daemon", "status", "--mcp"]) == 0
    assert "mcp: loaded" in capsys.readouterr().out


def test_daemon_backup_lifecycle(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_launchd, "uninstall_backup", lambda: None)
    assert main(["daemon", "uninstall", "--backup"]) == 0
    assert "uninstalled" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "restart_backup", lambda: None)
    assert main(["daemon", "restart", "--backup"]) == 0
    assert "restarted" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "backup_status", lambda: "backup: loaded")
    assert main(["daemon", "status", "--backup"]) == 0
    assert "backup: loaded" in capsys.readouterr().out


def test_daemon_backup_install_passes_notify_url(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        _launchd, "install_backup",
        lambda dest, home=None, **kw: seen.update(dest=dest, home=home, **kw)
        or "/plist/backup.plist",
    )
    rc = main(["daemon", "install", "--backup", "--dest", "/vol/bak",
               "--at", "04:00", "--notify-url", "http://n"])
    assert rc == 0
    assert seen["notify_url"] == "http://n" and seen["hour"] == 4 and seen["minute"] == 0
    assert "nightly at 04:00" in capsys.readouterr().out


def test_daemon_watcher_install_with_web(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        _launchd, "install_watcher",
        lambda home, *, web, web_port: seen.update(home=home, web=web, web_port=web_port)
        or "/plist/watcher.plist",
    )
    rc = main(["daemon", "install", "--home", "/h"])  # web defaults on
    assert rc == 0
    assert seen == {"home": "/h", "web": True, "web_port": 8787}
    out = capsys.readouterr().out
    assert "web viewer: http://127.0.0.1:8787" in out


def test_daemon_watcher_install_no_web(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        _launchd, "install_watcher",
        lambda home, *, web, web_port: seen.update(web=web) or "/plist",
    )
    rc = main(["daemon", "install", "--no-web"])
    assert rc == 0
    assert seen == {"web": False}
    assert "web viewer" not in capsys.readouterr().out


def test_daemon_watcher_uninstall_restart_status(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_launchd, "uninstall_watcher", lambda: None)
    assert main(["daemon", "uninstall"]) == 0
    assert "uninstalled" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "restart_watcher", lambda: None)
    assert main(["daemon", "restart"]) == 0
    assert "restarted" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "watcher_status", lambda: "watcher: loaded")
    assert main(["daemon", "status"]) == 0
    assert "watcher: loaded" in capsys.readouterr().out


# ── daemon: librarian / gardener drain agents ────────────────────────────────


def test_daemon_librarian_lifecycle(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(_launchd, "resolved_librarian_interval", lambda home: 1800)
    monkeypatch.setattr(
        _launchd, "install_librarian",
        lambda home, *, interval: seen.update(home=home, interval=interval)
        or "/plist/librarian.plist",
    )
    rc = main(["daemon", "install", "--librarian", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "interval": 1800}
    out = capsys.readouterr().out
    assert f"installed {_launchd.LIBRARIAN_LABEL}" in out
    assert "every 30 min" in out

    monkeypatch.setattr(_launchd, "uninstall_librarian", lambda: None)
    assert main(["daemon", "uninstall", "--librarian"]) == 0
    assert f"uninstalled {_launchd.LIBRARIAN_LABEL}" in capsys.readouterr().out

    # A scheduled one-shot has nothing resident to kick — restart is guidance.
    assert main(["daemon", "restart", "--librarian"]) == 0
    out = capsys.readouterr().out
    assert "nothing resident to restart" in out
    assert "archive curate librarian" in out

    monkeypatch.setattr(_launchd, "librarian_status", lambda: "librarian: scheduled")
    assert main(["daemon", "status", "--librarian"]) == 0
    assert "librarian: scheduled" in capsys.readouterr().out


def test_daemon_gardener_lifecycle(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        _launchd, "install_gardener",
        lambda home, *, hour, minute: seen.update(home=home, hour=hour, minute=minute)
        or "/plist/gardener.plist",
    )
    # An explicit --at wins over the resolved schedule.
    rc = main(["daemon", "install", "--gardener", "--at", "06:30", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "hour": 6, "minute": 30}
    assert "daily at 06:30" in capsys.readouterr().out

    # Without --at the cadence resolves from config.json / the defaults.
    monkeypatch.setattr(_launchd, "resolved_gardener_schedule", lambda home: (5, 0))
    assert main(["daemon", "install", "--gardener"]) == 0
    assert "daily at 05:00" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "uninstall_gardener", lambda: None)
    assert main(["daemon", "uninstall", "--gardener"]) == 0
    assert f"uninstalled {_launchd.GARDENER_LABEL}" in capsys.readouterr().out

    monkeypatch.setattr(_launchd, "gardener_status", lambda: "gardener: scheduled")
    assert main(["daemon", "status", "--gardener"]) == 0
    assert "gardener: scheduled" in capsys.readouterr().out


# ── curate: the by-hand drain ─────────────────────────────────────────────────


def test_curate_dispatches_to_the_drain(monkeypatch) -> None:
    from thread_archive import _curation

    seen = {}

    def fake_run(kind, home, *, batch, timeout):
        seen.update(kind=kind, home=home, batch=batch, timeout=timeout)
        return 0

    monkeypatch.setattr(_curation, "run", fake_run)
    rc = main(["curate", "gardener", "--home", "/h", "--batch", "5", "--timeout", "60"])
    assert rc == 0
    assert seen == {"kind": "gardener", "home": "/h", "batch": 5, "timeout": 60}


# ── reindex error branch ──────────────────────────────────────────────────────


def test_reindex_refused_returns_1(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")

    def boom(**kw):
        raise RuntimeError("no disk room")

    monkeypatch.setattr(api, "reindex", boom)
    rc = main(["reindex", "--home", str(tmp_path / "arc")])
    assert rc == 1
    assert "reindex refused: no disk room" in capsys.readouterr().err


def test_reindex_success_prints_counts(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")
    seen = {}
    monkeypatch.setattr(
        api, "reindex",
        lambda **kw: seen.update(kw) or {"threads": 2, "events": 9},
    )
    rc = main(["reindex", "--vectors", "--salvage", "--home", str(tmp_path / "arc")])
    assert rc == 0
    assert seen["vectors"] is True and seen["salvage"] is True
    out = capsys.readouterr().out
    assert "threads" in out and "events" in out and "done" in out


# ── backup: full warning surface ──────────────────────────────────────────────


def test_backup_all_warnings_returns_1(monkeypatch, capsys) -> None:
    res = {
        "truth_dir": "t", "dest": "d", "files_copied": 3, "bytes_copied": 5 * 1024 * 1024,
        "verify_ok": False, "deletions_skipped": 2, "shrinks_skipped": 1,
        "shrink_sample": ["a.jsonl"], "mirror_complete": True,
        "generation_created": "2026-07-15T00-00-00", "generations_kept": 7,
        "generations_pruned": 1, "generation_error": "snap failed",
        "rehomed_twins_deleted": 4,
    }
    monkeypatch.setattr(api, "backup", lambda dest, **kw: res)
    rc = main(["backup", "/dest"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "generation:" in out
    assert "WARNING: generation snapshot failed" in out
    assert "pre-backup verify FAILED" in out
    assert "rebalance twins: 4" in out
    assert "stale destination files kept" in out
    assert "SHRINK GUARD: 1" in out


def _backup_clean_base() -> dict:
    return {
        "truth_dir": "t", "dest": "d", "files_copied": 1, "bytes_copied": 0,
        "verify_ok": True, "deletions_skipped": 0, "shrinks_skipped": 0,
        "mirror_complete": True,
    }


def test_backup_bundle_error_fails_the_run(monkeypatch, capsys) -> None:
    # A stale .recovery/ restores yesterday's keyring — a failed run, not a footnote.
    res = {**_backup_clean_base(), "bundle_error": "smb down"}
    monkeypatch.setattr(api, "backup", lambda dest, **kw: res)
    rc = main(["backup", "/dest"])
    assert rc == 1
    assert "recovery bundle sync failed (smb down)" in capsys.readouterr().out


def test_backup_bundle_keyring_included(monkeypatch, capsys) -> None:
    res = {**_backup_clean_base(), "bundle_files": 3, "bundle_copied": 2,
           "bundle_deleted": 1, "keyring_in_bundle": True}
    monkeypatch.setattr(api, "backup", lambda dest, **kw: res)
    assert main(["backup", "/dest"]) == 0
    out = capsys.readouterr().out
    assert "recovery bundle: 3 file(s) (2 copied, 1 removed; keyring included)" in out


def test_backup_bundle_keyring_opted_out(monkeypatch, capsys) -> None:
    res = {**_backup_clean_base(), "bundle_files": 2, "bundle_copied": 0,
           "bundle_deleted": 0, "keyring_in_bundle": False, "keyring_opted_out": True}
    monkeypatch.setattr(api, "backup", lambda dest, **kw: res)
    assert main(["backup", "/dest"]) == 0
    assert "keyring EXCLUDED — config opt-out" in capsys.readouterr().out


# ── verify: parse errors, fts, deep samples, hashes, backup error ─────────────


def _verify_rich_failure() -> dict:
    return {
        "ok": False,
        "failed_components": ["drift_events", "fts_orphans"],
        "failure_log": "/home/verify-failures.jsonl",
        "truth": {
            "threads": 3, "events": 10, "events_effective": 8,
            "duplicate_id_lines": 1, "duplicate_content_lines": 1, "parse_errors": 2,
            "parse_errors_torn_tail": 1, "parse_errors_interior": 1,
            "parse_error_sample": ["truth/threads/x.jsonl:5"],
        },
        "index": {"threads": 3, "events": 9, "kg_events": 4,
                  "quick_check": "ok", "check": "quick_check"},
        "drift": {"threads": 0, "events": -1, "kg_events": 0},
        "fts": {"shadow_rows": 9, "fts5_rows": 8, "orphan_rows": 2},
        "deep": {
            "watermark": 9, "events_index_only": 1, "index_only_sample": [7],
            "events_missing_from_index": 1, "missing_sample": [3],
            "events_key_mismatch": 1, "key_mismatch_sample": [4],
            "events_superseded_twins": 1,
            "thread_meta_mismatch": 2, "thread_meta_sample": [11, 12],
            "kg": {"index_only": 1, "truth_only": 0, "content_mismatch": 1},
            "dangling": {"link_endpoints": 1, "citation_events": 0,
                         "citation_thread_mismatch": 0, "event_threads": 1},
            "duplicate_content_pairs_index": 1,
            "fts": {"orphan_rows": 2, "shadow_rows": 9, "fts5_rows": 8,
                    "unindexed_events": 1, "unindexed_sample": [6],
                    "empty_extract_events": 1},
        },
        "hashes": {
            "truth": {"checked": 8, "mismatched": 1, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": [1]},
            "index": {"checked": 8, "mismatched": 1, "unhashed_keys": 0, "no_key": 0,
                      "mismatch_sample": [2]},
            "cross": {"compared": 8, "mismatched": 1, "mismatch_sample": [3]},
            "previous": {"at": "2026-07-14T00:00:00Z"},
            "delta": {"truth_mismatched": 1, "index_mismatched": 1, "cross_mismatched": 1},
        },
        "backup": {"dest": "/mirror", "error": "mirror unreadable"},
    }


def test_verify_rich_failure_all_branches(monkeypatch, capsys) -> None:
    monkeypatch.setattr(api, "verify", lambda **kw: _verify_rich_failure())
    rc = main(["verify", "--deep", "--hashes", "--backup", "/mirror"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "torn tails=1 interior=1" in out
    assert "parse error sample" in out
    assert "fts:   shadow=9 fts5=8 orphans=2" in out
    assert "thread metadata drift" in out
    assert "unindexed sample" in out
    assert "missing sample" in out
    assert "index-only sample" in out
    assert "key-mismatch sample" in out
    assert "hashes[truth]" in out and "mismatch sample" in out
    assert "hashes[cross]: compared=8 mismatched=1" in out
    assert "hashes delta vs 2026-07-14T00:00:00Z" in out
    assert "backup[/mirror]: mirror unreadable" in out
    assert "FAILED: drift_events, fts_orphans" in out
    assert "full result appended to /home/verify-failures.jsonl" in out


def test_verify_backup_scan_and_shrink(monkeypatch, capsys) -> None:
    res = {
        "ok": True,
        "truth": {"threads": 1, "events": 2, "events_effective": 2,
                  "duplicate_id_lines": 0, "duplicate_content_lines": 0, "parse_errors": 0},
        "index": {"threads": 1, "events": 2, "kg_events": 0,
                  "quick_check": "ok", "check": "quick_check"},
        "drift": {"threads": 0, "events": 0, "kg_events": 0},
        "fts": {"shadow_rows": 2, "fts5_rows": 2, "orphan_rows": 0},
        "backup": {
            "dest": "/mirror", "coverage": 0.9876,
            "scan": {"threads": 1, "events_effective": 2, "parse_errors": 0},
            "effective_drop": {"previous": 5, "current": 2, "previous_at": "2026-07-14T00:00:00Z"},
            "hashes": {"checked": 2, "mismatched": 1, "unhashed_keys": 0, "no_key": 0,
                       "mismatch_sample": [9]},
        },
    }
    monkeypatch.setattr(api, "verify", lambda **kw: res)
    rc = main(["verify", "--backup", "/mirror"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backup[/mirror]: threads=1" in out and "coverage=0.9876" in out
    assert "MIRROR SHRANK: 5 → 2 effective" in out
    assert "backup hashes: checked=2 mismatched=1" in out
    assert "mismatch sample: [9]" in out
    assert "OK" in out


def test_verify_backup_hashes_clean(monkeypatch, capsys) -> None:
    """Backup mirror with hashes present but no mismatch — the clean-hash branch."""
    res = {
        "ok": True,
        "truth": {"threads": 1, "events": 2, "events_effective": 2,
                  "duplicate_id_lines": 0, "duplicate_content_lines": 0, "parse_errors": 0},
        "index": {"threads": 1, "events": 2, "kg_events": 0,
                  "quick_check": "ok", "check": "quick_check"},
        "drift": {"threads": 0, "events": 0, "kg_events": 0},
        "fts": {"shadow_rows": 2, "fts5_rows": 2, "orphan_rows": 0},
        "backup": {
            "dest": "/mirror", "coverage": 1.0,
            "scan": {"threads": 1, "events_effective": 2, "parse_errors": 0},
            "hashes": {"checked": 2, "mismatched": 0, "unhashed_keys": 0, "no_key": 0,
                       "mismatch_sample": []},
        },
    }
    monkeypatch.setattr(api, "verify", lambda **kw: res)
    assert main(["verify", "--backup", "/mirror"]) == 0
    out = capsys.readouterr().out
    assert "backup hashes: checked=2 mismatched=0" in out
    assert "mismatch sample" not in out  # clean → no sample line


# ── restore-drill: report + failure branches ─────────────────────────────────


def test_restore_drill_full_report_skipped_smoke(monkeypatch, capsys) -> None:
    res = {
        "ok": True, "seconds": 2.5, "coverage": 0.9999,
        "mirror": {"threads": 4, "events_effective": 40, "parse_errors": 0},
        "rebuilt": {"threads": 4, "events": 40, "fts": 40},
        "smoke": {"skipped": "no models installed"},
    }
    monkeypatch.setattr(api, "restore_drill", lambda dest, **kw: res)
    rc = main(["restore-drill", "/mirror"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mirror: threads=4 effective=40" in out
    assert "rebuilt: threads=4 events=40 fts=40" in out
    assert "smoke:  skipped (no models installed)" in out
    assert "OK (2.5s)" in out


def test_restore_drill_failure_with_smoke(monkeypatch, capsys) -> None:
    res = {
        "ok": False, "seconds": 1.0, "error": "rebuild aborted",
        "mirror": {"threads": 1, "events_effective": 1, "parse_errors": 3},
        "rebuilt": {"threads": 1, "events": 1},
        "smoke": {"read_ok": True, "search_ok": False, "token": "hello", "error": "no hit"},
        "drill_home": "/tmp/drill-abc",
    }
    monkeypatch.setattr(api, "restore_drill", lambda dest, **kw: res)
    rc = main(["restore-drill", "/mirror", "--keep-home"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED: rebuild aborted" in out
    assert "smoke:  read=ok search=FAILED" in out
    assert "token 'hello'" in out and "error: no hit" in out
    assert "drill home kept: /tmp/drill-abc" in out
    assert "RESTORE DRILL FAILED (1.0s)" in out


def test_restore_drill_bundle_absent(monkeypatch, capsys) -> None:
    res = {"ok": True, "seconds": 1.0, "bundle": {"present": False}}
    monkeypatch.setattr(api, "restore_drill", lambda dest, **kw: res)
    assert main(["restore-drill", "/mirror"]) == 0
    assert "bundle: ABSENT" in capsys.readouterr().out


def test_restore_drill_bundle_present_unreadable_keyring(monkeypatch, capsys) -> None:
    res = {
        "ok": True, "seconds": 1.0,
        "bundle": {"present": True, "config": True, "keyring_keys": None,
                   "retained_exports": 2, "keyring_unreadable": True},
    }
    monkeypatch.setattr(api, "restore_drill", lambda dest, **kw: res)
    assert main(["restore-drill", "/mirror"]) == 0
    out = capsys.readouterr().out
    assert "bundle: config=yes keyring keys=none retained exports=2 (keyring UNREADABLE)" in out


# ── restore: skipped-smoke, damaged home, and failure branches ────────────────
# The real restore path (mirror + rebuilt + working smoke + OK line) is driven
# end-to-end in test_restore.py; these stub api.restore to reach the formatting
# branches a happy restore can't produce.


def test_restore_skipped_smoke_and_damaged_home(monkeypatch, capsys) -> None:
    res = {
        "ok": True, "seconds": 2.0,
        "mirror": {"threads": 1, "events_effective": 1, "parse_errors": 0},
        "rebuilt": {"threads": 1, "events": 1, "fts": 1},
        "smoke": {"skipped": "no models installed"},
        "damaged_home": "/h/x.damaged-2026-07-15T00-00-00",
    }
    monkeypatch.setattr(api, "restore", lambda dest, to, **kw: res)
    rc = main(["restore", "/mirror", "--to", "/h/x", "--replace"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mirror: threads=1" in out
    assert "rebuilt: threads=1 events=1 fts=1" in out
    assert "smoke:  skipped (no models installed)" in out
    assert "previous home set aside (preserved): /h/x.damaged-2026-07-15T00-00-00" in out
    assert "OK — restored to /h/x (2.0s)" in out


def test_restore_failed_no_mirror_no_smoke_error(monkeypatch, capsys) -> None:
    """A failed restore with neither mirror nor rebuilt scanned, no smoke, and an
    error — the pure failure surface, from a named generation."""
    res = {"ok": False, "seconds": 1.0, "error": "rebuild aborted"}
    monkeypatch.setattr(api, "restore", lambda dest, to, **kw: res)
    rc = main(["restore", "/mirror", "--to", "/h/x", "--generation", "2026-07-14T00-00-00"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "generation 2026-07-14T00-00-00" in out  # the generation-qualified src line
    assert "mirror:" not in out and "rebuilt:" not in out
    assert "FAILED: rebuild aborted" in out
    assert "RESTORE FAILED (1.0s)" in out


def test_restore_bundle_installed_with_error(monkeypatch, capsys) -> None:
    res = {
        "ok": True, "seconds": 1.0,
        "bundle": {"config": True, "keyring": True, "retained_exports": 2,
                   "error": "keyring locked"},
    }
    monkeypatch.setattr(api, "restore", lambda dest, to, **kw: res)
    assert main(["restore", "/mirror", "--to", "/h/x"]) == 0
    out = capsys.readouterr().out
    assert "bundle: installed config, keyring, 2 retained export(s) (ERROR: keyring locked)" in out


# ── nightly: full report, drill error ─────────────────────────────────────────


def test_nightly_verify_failed_drill_ok(monkeypatch, capsys) -> None:
    res = {
        "backup": {"files_copied": 1, "bytes_copied": 2 * 1024 * 1024},
        "escalations": {"deep": True, "hashes": True},
        "verify": {"ok": False, "failed_components": ["drift_events"],
                   "drift": {"events": -2}, "truth": {"parse_errors": 1},
                   "failure_log": "/home/verify-failures.jsonl"},
        "drill": {"ok": True, "coverage": 0.9700, "seconds": 3},
        "notify_error": "connection refused",
        "ok": False, "failed_stages": ["verify"],
    }
    monkeypatch.setattr(api, "nightly", lambda dest, **kw: res)
    rc = main(["nightly", "/dest"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "backup: 1 files (2.0 MB copied)" in out
    assert "verify [deep+hashes]: FAILED (drift_events)" in out
    assert "full result appended to /home/verify-failures.jsonl" in out
    assert "restore drill: ok coverage=0.9700 (3s)" in out
    assert "notify: could not deliver failure notification (connection refused)" in out
    assert "NIGHTLY FAILED: verify" in out


def test_nightly_backup_error_and_drill_error(monkeypatch, capsys) -> None:
    res = {
        "backup": {"error": "dest not mounted"},
        "escalations": {"deep": False, "hashes": False},
        "verify": {"ok": True, "drift": {"events": 0}, "truth": {"parse_errors": 0}},
        "drill": {"error": "throwaway home failed"},
        "ok": False, "failed_stages": ["backup", "drill"],
    }
    monkeypatch.setattr(api, "nightly", lambda dest, **kw: res)
    rc = main(["nightly", "/dest"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "backup: ERROR dest not mounted" in out
    assert "verify [shallow]: ok" in out
    assert "restore drill: ERROR throwaway home failed" in out
    assert "NIGHTLY FAILED: backup, drill" in out


def test_nightly_verify_failed_no_log(monkeypatch, capsys) -> None:
    """Verify failed but no failure_log recorded — skips the log-pointer line."""
    res = {
        "backup": {"files_copied": 1, "bytes_copied": 0},
        "escalations": {"deep": False, "hashes": False},
        "verify": {"ok": False, "failed_components": ["fts_orphans"],
                   "drift": {"events": 0}, "truth": {"parse_errors": 0}},
        "ok": False, "failed_stages": ["verify"],
    }
    monkeypatch.setattr(api, "nightly", lambda dest, **kw: res)
    assert main(["nightly", "/dest"]) == 1
    out = capsys.readouterr().out
    assert "verify [shallow]: FAILED (fts_orphans)" in out
    assert "full result appended" not in out


def test_nightly_verify_error_branch(monkeypatch, capsys) -> None:
    res = {
        "backup": {"files_copied": 0, "bytes_copied": 0},
        "escalations": {"deep": False, "hashes": True},
        "verify": {"error": "index locked"},
        "ok": False, "failed_stages": ["verify"],
    }
    monkeypatch.setattr(api, "nightly", lambda dest, **kw: res)
    rc = main(["nightly", "/dest"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "verify [hashes]: ERROR index locked" in out


# ── repair: applied (non-dry-run) branches ───────────────────────────────────


def test_repair_applied_with_samples(monkeypatch, capsys) -> None:
    res = {
        "dry_run": False, "fragments_quarantined": 3, "files_damaged": 2,
        "damaged_sample": ["truth/threads/x.jsonl:9"], "quarantine_file": "/home/quarantine.jsonl",
        "events_restored_from_index": 4, "kg_events_restored": 1, "thread_records_restored": 2,
    }
    seen = {}
    monkeypatch.setattr(api, "repair", lambda **kw: seen.update(kw) or res)
    rc = main(["repair", "--home", "/h"])
    assert rc == 0
    assert seen == {"home": "/h", "dry_run": False}
    out = capsys.readouterr().out
    assert "quarantined 3 unparseable line(s) across 2 file(s)" in out
    assert "sample: ['truth/threads/x.jsonl:9']" in out
    assert "ledger: /home/quarantine.jsonl" in out
    assert "restored from index: 4 event(s)" in out
    assert "the repaired files shrank" in out
    assert "run `archive verify`" in out


# ── redact: false branches (no key_id / no scrubbed) + whole-thread list ──────


def test_redact_minimal_no_key(monkeypatch, capsys) -> None:
    """A redact result without a key_id / scrubbed counts / notes exercises the
    skip branches of the summary block."""
    monkeypatch.setattr(
        api, "redact",
        lambda thread_id, event_ids, **kw: {"events_redacted": 1, "thread_id": 2},
    )
    rc = main(["redact", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "redacted 1 event(s) in thread 2" in out
    assert "under key" not in out
    assert "reverse with" not in out


def test_redact_list_whole_thread_no_reason(monkeypatch, capsys) -> None:
    row = {"key_id": "k9", "thread_id": 3, "event_ids": [], "status": "redacted",
           "key": "held", "redacted_at": "2026-07-15T00:00:00Z"}
    monkeypatch.setattr(api, "redactions", lambda **kw: [row])
    rc = main(["redact", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "k9  thread 3  whole thread" in out
    assert "reason:" not in out


def test_redact_restore_key_dispatches(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        api, "redact_restore_key",
        lambda kid, key, **kw: seen.update(kid=kid, key=key, **kw),
    )
    rc = main(["redact", "--restore-key", "k1", "b64==", "--home", "/h"])
    assert rc == 0
    assert seen == {"kid": "k1", "key": "b64==", "home": "/h"}
    assert "key k1 restored to the keyring" in capsys.readouterr().out


# ── unredact: notes branch ────────────────────────────────────────────────────


def test_unredact_prints_notes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        api, "unredact",
        lambda kid, **kw: {"events_restored": 2, "thread_id": 7,
                           "notes": ["provider store keeps its plaintext"]},
    )
    rc = main(["unredact", "k1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "restored 2 event(s) in thread 7" in out
    assert "note: provider store keeps its plaintext" in out


# ── status: fully-populated ok + failed variants ─────────────────────────────


def _status_base(**over) -> dict:
    base = {
        "home": "/h", "truth_dir": "/h/truth", "index_path": "/h/index.db",
        "threads": 5, "events": 50, "fts_indexed": 50,
    }
    base.update(over)
    return base


def test_status_all_ok(monkeypatch, capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_verify={"ok": True, "at": old},
        last_backup={"ok": True, "dest": "/vol/bak", "at": old},
        last_restore_drill={"ok": True, "coverage": 0.99, "at": old},
        last_nightly={"ok": True, "dest": "/vol/bak", "at": old},
        last_coverage={"ok": True, "sources_checked": 6, "at": old},
        last_watch_pass={"at": old, "sources": {"cc": {"events": 3, "parse_errors": 2}}},
        last_watch_errors={"at": old, "count_since_start": 4,
                           "errors": ["e1", "e2", "e3", "e4"]},
    )
    monkeypatch.setattr(api, "status", lambda **kw: st)
    rc = main(["status", "--home", "/h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verify:  ok" in out
    assert "backup:  ok → /vol/bak" in out
    assert "nightly: ok → /vol/bak" in out
    assert "drill:   ok coverage=0.99" in out
    assert "coverage: ok (6 sources)" in out
    assert "3 events since pass-owner start" in out
    assert "2 PARSE ERRORS" in out
    assert "poll errors seen" in out
    # only the first 3 watch errors are echoed
    assert "e3" in out and "e4" not in out


def test_status_green_coverage_still_shows_warnings(monkeypatch, capsys) -> None:
    # A stale export is a capture hole in the making; a green coverage check must
    # not swallow the warning that says so.
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_coverage={"ok": True, "sources_checked": 6, "at": old,
                       "warnings": ["chatgpt-export: last export 127d ago"]},
    )
    monkeypatch.setattr(api, "status", lambda **kw: st)
    rc = main(["status", "--home", "/h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "coverage: ok (6 sources, 1 warning(s))" in out
    assert "chatgpt-export: last export 127d ago" in out


def test_status_all_failed(monkeypatch, capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_verify={"ok": False, "failed": ["drift"], "at": old},
        last_backup={"ok": False, "dest": "/vol/bak", "at": old},
        last_restore_drill={"ok": False, "coverage": 0.1, "at": old},
        last_nightly={"ok": False, "dest": "/vol/off",
                      "failed_stages": ["backup", "restore-drill"], "at": old},
        last_coverage={"ok": False, "at": old, "failed": ["m1", "m2", "m3", "m4"]},
        last_watch_pass={"at": old, "sources": {}},
    )
    monkeypatch.setattr(api, "status", lambda **kw: st)
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "verify:  FAILED (drift)" in out
    assert "backup:  FAILED → /vol/bak" in out
    assert "nightly: FAILED (backup, restore-drill) → /vol/off" in out
    assert "drill:   FAILED" in out
    assert "coverage: FAILED" in out
    assert "m1" in out and "m3" in out and "m4" not in out  # capped at 3
    assert "last pass" in out  # watch pass with no parse errors
    assert "no pass recorded" not in out


# ── coverage: source states, disabled/unwatched, skips, failure ──────────────


def test_coverage_full_surface_failed(monkeypatch, capsys) -> None:
    result = {
        "ok": False,
        "failed": ["cursor stale > 48h"],
        "warnings": ["opencode newest event is 30h old"],
        "sources": {
            "claude-code": {"failed": None, "warning": None,
                            "store_latest": "2026-07-15", "newest_event_at": "2026-07-15",
                            "history": 100},
            "codex": {"failed": "stale", "warning": None,
                      "store_latest": None, "newest_event_at": None, "history": 5},
            "grok": {"failed": None, "warning": "lagging",
                     "store_latest": "2026-07-14", "newest_event_at": "2026-07-14",
                     "history": 20},
        },
        "disabled": {"antigravity": {"history": 0}},
        "unwatched": {"demo-harness": {"newest_event_at": None}},
        "skips": {"total": 7, "recent": 2, "recent_lines": 3, "days": 7.0},
        "drift": {"total": 0, "recent": 0, "recent_findings": 0, "days": 7.0},
    }
    monkeypatch.setattr(api, "check_coverage", lambda **kw: result)
    rc = main(["coverage"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "claude-code" in out and "ok" in out
    assert "codex" in out and "stale" in out
    assert "grok" in out and "lagging" in out
    assert "antigravity" in out and "disabled" in out
    assert "demo-harness" in out and "unwatched" in out
    assert "skips: 7 ledger records" in out
    assert "warning: opencode newest event is 30h old" in out
    assert "FAILED:" in out
    assert "cursor stale > 48h" in out


# ── main() with no subcommand prints help ────────────────────────────────────


def test_main_no_command_prints_help(capsys) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "archive" in out
