"""Branch-coverage tests for the ``archive`` CLI (:mod:`thread_archive.cli`).

Companion to ``test_cli_smoke.py``, which drives the verbs end-to-end over a
seeded archive. This file covers the branches a real run cannot reach, two ways:

* the ``report_*`` functions — each verb's operator report is a pure function of
  the result dict and its exit code, so every warning/sample/failure line is
  driven directly with the shape that produces it, no ``_api`` involved;
* the verbs whose boundary is outside the archive — launchd (the operator's live
  session), the watcher's long-running loop, the self-update git clone — which
  are stubbed at the module attribute the handler resolves.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thread_archive import _api as api
from thread_archive import _launchd, _truth, _update, _watcher, _web, cli
from thread_archive.cli import main

from .helpers import (
    cc_assistant,
    cc_user,
    event_count,
    import_cc_session,
    one_thread_file,
    write_jsonl,
)

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


def test_self_throttle_really_renices_the_process() -> None:
    """``nice()`` is one-way, so this runs in a child: the real throttle (CPU
    nice + on macOS the io-policy syscall) lands on a process we can throw away,
    and the env opt-out really skips it."""
    probe = (
        "import os, sys;"
        "from thread_archive.cli import _self_throttle;"
        "_self_throttle();"
        "print(os.nice(0))"
    )

    def run(**env):
        return subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, env={**os.environ, **env},
        )

    # Relative to this process's own niceness, not a fixed number: nice is
    # inherited, so the launcher sets the floor — thread-ci runs its suites at
    # nice 10, and 19 is the kernel's ceiling.
    base = os.nice(0)

    throttled = run(THREAD_ARCHIVE_NO_THROTTLE="")
    assert throttled.returncode == 0, throttled.stderr
    assert int(throttled.stdout.strip()) == min(base + 10, 19)

    opted_out = run(THREAD_ARCHIVE_NO_THROTTLE="1")
    assert opted_out.returncode == 0, opted_out.stderr
    assert int(opted_out.stdout.strip()) == base


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


def test_import_line_stream_summary(archive_home, tmp_path, capsys) -> None:
    """A line-stream provider's summary names the thread it landed in and the
    events it created — read back off the real import, not a stub's numbers."""
    session = tmp_path / "session.jsonl"
    write_jsonl(session, [cc_user("cli"), cc_assistant("cli")])

    rc = main(["import", str(session), "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported {session} (claude-code): thread=" in out
    assert "new=True" in out
    events = int(out.split("events=")[1].split()[0])
    assert events == event_count() > 0

    # a second import of the same file is the same thread, no longer new
    assert main(["import", str(session), "--home", str(archive_home)]) == 0
    assert "new=False" in capsys.readouterr().out


def _cursor_store(path) -> None:
    """A minimal cursor ``state.vscdb``: one composer, one user + one agent bubble."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        ("composerData:comp1", json.dumps({
            "name": "Cursor Chat", "lastUpdatedAt": 1700000000000,
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1},
                                            {"bubbleId": "b2", "type": 2}]})),
        ("bubbleId:comp1:b1", json.dumps(
            {"type": 1, "text": "hello cursor", "createdAt": 1700000000000})),
        ("bubbleId:comp1:b2", json.dumps(
            {"type": 2, "text": "hi from cursor", "createdAt": 1700000001000})),
    ])
    conn.commit()
    conn.close()


def test_import_db_scanner_summary(archive_home, tmp_path, capsys) -> None:
    """The cursor/opencode DB scanners return a result whose ``vars()`` is the
    printed summary (many sessions per file)."""
    db = tmp_path / "state.vscdb"
    _cursor_store(db)

    rc = main(["import", str(db), "--provider", "cursor", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported {db} (cursor):" in out
    assert "processed=1 imported=1" in out
    assert f"events_created={event_count()}" in out and event_count() > 0


def test_import_unknown_provider_direct_call() -> None:
    """The registry guard in cmd_import (argparse ``choices`` normally blocks an
    unknown provider before the handler, so exercise the guard directly)."""
    ns = argparse.Namespace(provider="bogus-provider", path="/x", home=None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_import(ns)
    assert "unknown provider 'bogus-provider'" in str(exc.value)


# ── providers ─────────────────────────────────────────────────────────────────
# Driven against the real registry: the verb's whole job is reporting what the
# registry holds, so stubbing it would assert only the format string.


def _providers_rows(out: str) -> dict[str, str]:
    """The printed table as ``{provider name: rest of its line}``."""
    return {
        line.split()[0]: line.split(maxsplit=1)[1]
        for line in out.splitlines()
        if line and not line.startswith("(")
    }


def test_providers_lists_sources_not_mechanisms(archive_home, capsys) -> None:
    rc = main(["providers", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _providers_rows(out)
    # Archive's own machinery is held back unless asked for.
    assert "export-drop" not in rows and "cc-exthost" not in rows
    assert "on" in rows["claude-code"] and "Claude Code" in rows["claude-code"]
    assert "line-stream" in rows["claude-code"]
    assert "db-scan" in rows["cursor"]
    # An export-only provider has no live store to poll; both traits are named.
    assert "export:" in rows["chatgpt"] and "no live store" in rows["chatgpt"]
    # A watched provider with no importer kind and no export carries no traits,
    # so its line ends at the label — no empty parenthetical.
    assert rows["cowork"].rstrip().endswith("Cowork")
    assert "(--all also lists archive's own machinery)" in out


def test_providers_all_includes_mechanisms(archive_home, capsys) -> None:
    rc = main(["providers", "--all", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _providers_rows(out)
    assert "mechanism" in rows["export-drop"]
    assert "follows claude-code" in rows["cc-exthost"]
    # The footer only advertises --all when it wasn't given.
    assert "--all also lists" not in out


def test_providers_marks_disabled_and_followers_off(archive_home, capsys) -> None:
    """A disabled source reads ``off``, and so does the recovery pass that follows
    it — the follower has no independent meaning once its primary is off."""
    (archive_home / "config.json").write_text(
        '{"sources": {"claude-code": {"enabled": false}}}', encoding="utf-8"
    )
    rc = main(["providers", "--all", "--home", str(archive_home)])
    assert rc == 0
    rows = _providers_rows(capsys.readouterr().out)
    assert rows["claude-code"].startswith("off")
    assert rows["cc-exthost"].startswith("off")
    assert rows["codex"].startswith("on")


# ── import-export ─────────────────────────────────────────────────────────────


def test_import_export_imports_a_real_export(archive_home, tmp_path, capsys) -> None:
    """`archive import-export` unpacks a real claude.ai export ZIP into the
    archive, and --force reaches the importer: a second pass skips what is
    already there unless it is told to reimport."""
    conv = {
        "uuid": "conv-1", "name": "Exported", "created_at": "2026-01-01T10:00:00Z",
        "updated_at": "2026-01-01T10:00:10Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "hello export",
             "created_at": "2026-01-01T10:00:00Z"},
            {"uuid": "m2", "sender": "assistant", "text": "hi from the export",
             "created_at": "2026-01-01T10:00:05Z"},
        ],
    }
    def write_export(path, conversation) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("conversations.json", json.dumps([conversation]))
            zf.writestr("users.json", json.dumps([{"uuid": "user-1"}]))

    export = tmp_path / "claude-export.zip"
    write_export(export, conv)

    rc = main(["import-export", str(export), "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"imported export {export}: processed=1 imported=1 skipped=0 events=" in out
    assert event_count() > 0
    # checkpointed: the imported thread's metadata really reached the truth log
    assert any("Exported" in f.read_text(encoding="utf-8")
               for f in (archive_home / "truth" / "threads").rglob("*.jsonl"))

    # a later export of the same conversation, one turn longer
    grown = {**conv, "chat_messages": [*conv["chat_messages"],
             {"uuid": "m3", "sender": "human", "text": "one more turn",
              "created_at": "2026-01-01T10:01:00Z"}]}
    write_export(export, grown)

    assert main(["import-export", str(export), "--home", str(archive_home)]) == 0
    assert "processed=1 imported=0 skipped=1 events=0" in capsys.readouterr().out

    before = event_count()
    assert main(["import-export", str(export), "--force",
                 "--home", str(archive_home)]) == 0
    assert "processed=1 imported=1 skipped=0" in capsys.readouterr().out
    assert event_count() > before  # --force really re-read the export


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


# ── reindex error branch ──────────────────────────────────────────────────────


def _gut_the_truth(archive_home) -> None:
    """Empty the archive's one thread file — a truth that lost committed records
    the index still holds, the state the publication guard exists for."""
    from thread_archive._truth import jsonl_log

    one_thread_file(archive_home).write_text("", encoding="utf-8")
    jsonl_log.reset_handles()


def test_reindex_refuses_a_lossy_rebuild(archive_home, tmp_path, monkeypatch, capsys) -> None:
    """A rebuild that would lose committed records is refused and the old index
    kept — the operator gets guidance on stderr, not a stack trace."""
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")
    import_cc_session(tmp_path)
    before = event_count()
    _gut_the_truth(archive_home)

    rc = main(["reindex", "--home", str(archive_home)])
    assert rc == 1
    assert "reindex refused:" in capsys.readouterr().err
    assert event_count() == before  # the old index is intact


def test_reindex_salvage_publishes_the_lossy_rebuild(
    archive_home, tmp_path, monkeypatch, capsys
) -> None:
    """--salvage reaches api.reindex: the same rebuild the default refuses is
    published, and the counts it printed are the ones it wrote."""
    monkeypatch.setenv("THREAD_ARCHIVE_NO_THROTTLE", "1")
    import_cc_session(tmp_path)
    _gut_the_truth(archive_home)

    rc = main(["reindex", "--vectors", "--salvage", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vectors=True" in out and "done" in out
    assert "events" in out and "threads" in out
    assert event_count() == 0  # the lossy rebuild really replaced the index


# ── backup: full warning surface ──────────────────────────────────────────────


def test_backup_all_warnings_returns_1(capsys) -> None:
    res = {
        "truth_dir": "t", "dest": "d", "files_copied": 3, "bytes_copied": 5 * 1024 * 1024,
        "verify_ok": False, "deletions_skipped": 2, "shrinks_skipped": 1,
        "shrink_sample": ["a.jsonl"], "mirror_complete": True,
        "generation_created": "2026-07-15T00-00-00", "generations_kept": 7,
        "generations_pruned": 1, "generation_error": "snap failed",
        "rehomed_twins_deleted": 4,
    }
    rc = cli.report_backup(res)
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


def test_backup_bundle_error_fails_the_run(capsys) -> None:
    # A stale .recovery/ restores yesterday's keyring — a failed run, not a footnote.
    res = {**_backup_clean_base(), "bundle_error": "smb down"}
    assert cli.report_backup(res) == 1
    assert "recovery bundle sync failed (smb down)" in capsys.readouterr().out


def test_backup_bundle_keyring_included(capsys) -> None:
    res = {**_backup_clean_base(), "bundle_files": 3, "bundle_copied": 2,
           "bundle_deleted": 1, "keyring_in_bundle": True}
    assert cli.report_backup(res) == 0
    out = capsys.readouterr().out
    assert "recovery bundle: 3 file(s) (2 copied, 1 removed; keyring included)" in out


def test_backup_bundle_keyring_opted_out(capsys) -> None:
    res = {**_backup_clean_base(), "bundle_files": 2, "bundle_copied": 0,
           "bundle_deleted": 0, "keyring_in_bundle": False, "keyring_opted_out": True}
    assert cli.report_backup(res) == 0
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


def test_verify_rich_failure_all_branches(capsys) -> None:
    rc = cli.report_verify(
        _verify_rich_failure(), deep=True, hashes=True, backup="/mirror"
    )
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


def test_verify_backup_scan_and_shrink(capsys) -> None:
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
    rc = cli.report_verify(res, backup="/mirror")
    assert rc == 0
    out = capsys.readouterr().out
    assert "backup[/mirror]: threads=1" in out and "coverage=0.9876" in out
    assert "MIRROR SHRANK: 5 → 2 effective" in out
    assert "backup hashes: checked=2 mismatched=1" in out
    assert "mismatch sample: [9]" in out
    assert "OK" in out


def test_verify_backup_hashes_clean(capsys) -> None:
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
    assert cli.report_verify(res, backup="/mirror") == 0
    out = capsys.readouterr().out
    assert "backup hashes: checked=2 mismatched=0" in out
    assert "mismatch sample" not in out  # clean → no sample line


# ── restore-drill: report + failure branches ─────────────────────────────────


def test_restore_drill_full_report_skipped_smoke(capsys) -> None:
    res = {
        "ok": True, "seconds": 2.5, "coverage": 0.9999,
        "mirror": {"threads": 4, "events_effective": 40, "parse_errors": 0},
        "rebuilt": {"threads": 4, "events": 40, "fts": 40},
        "smoke": {"skipped": "no models installed"},
    }
    rc = cli.report_restore_drill(res)
    assert rc == 0
    out = capsys.readouterr().out
    assert "mirror: threads=4 effective=40" in out
    assert "rebuilt: threads=4 events=40 fts=40" in out
    assert "smoke:  skipped (no models installed)" in out
    assert "OK (2.5s)" in out


def test_restore_drill_failure_with_smoke(capsys) -> None:
    res = {
        "ok": False, "seconds": 1.0, "error": "rebuild aborted",
        "mirror": {"threads": 1, "events_effective": 1, "parse_errors": 3},
        "rebuilt": {"threads": 1, "events": 1},
        "smoke": {"read_ok": True, "search_ok": False, "token": "hello", "error": "no hit"},
        "drill_home": "/tmp/drill-abc",
    }
    rc = cli.report_restore_drill(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED: rebuild aborted" in out
    assert "smoke:  read=ok search=FAILED" in out
    assert "token 'hello'" in out and "error: no hit" in out
    assert "drill home kept: /tmp/drill-abc" in out
    assert "RESTORE DRILL FAILED (1.0s)" in out


def test_restore_drill_bundle_absent(capsys) -> None:
    res = {"ok": True, "seconds": 1.0, "bundle": {"present": False}}
    assert cli.report_restore_drill(res) == 0
    assert "bundle: ABSENT" in capsys.readouterr().out


def test_restore_drill_bundle_present_unreadable_keyring(capsys) -> None:
    res = {
        "ok": True, "seconds": 1.0,
        "bundle": {"present": True, "config": True, "keyring_keys": None,
                   "retained_exports": 2, "keyring_unreadable": True},
    }
    assert cli.report_restore_drill(res) == 0
    out = capsys.readouterr().out
    assert "bundle: config=yes keyring keys=none retained exports=2 (keyring UNREADABLE)" in out


# ── restore: skipped-smoke, damaged home, and failure branches ────────────────
# The real restore path (mirror + rebuilt + working smoke + OK line) is driven
# end-to-end in test_restore.py; these stub api.restore to reach the formatting
# branches a happy restore can't produce.


def test_restore_skipped_smoke_and_damaged_home(capsys) -> None:
    res = {
        "ok": True, "seconds": 2.0,
        "mirror": {"threads": 1, "events_effective": 1, "parse_errors": 0},
        "rebuilt": {"threads": 1, "events": 1, "fts": 1},
        "smoke": {"skipped": "no models installed"},
        "damaged_home": "/h/x.damaged-2026-07-15T00-00-00",
    }
    rc = cli.report_restore(res, to="/h/x")
    assert rc == 0
    out = capsys.readouterr().out
    assert "mirror: threads=1" in out
    assert "rebuilt: threads=1 events=1 fts=1" in out
    assert "smoke:  skipped (no models installed)" in out
    assert "previous home set aside (preserved): /h/x.damaged-2026-07-15T00-00-00" in out
    assert "OK — restored to /h/x (2.0s)" in out


def test_restore_failed_no_mirror_no_smoke_error(capsys) -> None:
    """A failed restore with neither mirror nor rebuilt scanned, no smoke, and an
    error — the pure failure surface, from a named generation."""
    res = {"ok": False, "seconds": 1.0, "error": "rebuild aborted"}
    rc = cli.report_restore(res, to="/h/x")
    assert rc == 1
    out = capsys.readouterr().out
    assert "mirror:" not in out and "rebuilt:" not in out
    assert "FAILED: rebuild aborted" in out
    assert "RESTORE FAILED (1.0s)" in out


def test_restore_bundle_installed_with_error(capsys) -> None:
    res = {
        "ok": True, "seconds": 1.0,
        "bundle": {"config": True, "keyring": True, "retained_exports": 2,
                   "error": "keyring locked"},
    }
    assert cli.report_restore(res, to="/h/x") == 0
    out = capsys.readouterr().out
    assert "bundle: installed config, keyring, 2 retained export(s) (ERROR: keyring locked)" in out


# ── nightly: full report, drill error ─────────────────────────────────────────


def test_nightly_verify_failed_drill_ok(capsys) -> None:
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
    rc = cli.report_nightly(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "backup: 1 files (2.0 MB copied)" in out
    assert "verify [deep+hashes]: FAILED (drift_events)" in out
    assert "full result appended to /home/verify-failures.jsonl" in out
    assert "restore drill: ok coverage=0.9700 (3s)" in out
    assert "notify: could not deliver failure notification (connection refused)" in out
    assert "NIGHTLY FAILED: verify" in out


def test_nightly_backup_error_and_drill_error(capsys) -> None:
    res = {
        "backup": {"error": "dest not mounted"},
        "escalations": {"deep": False, "hashes": False},
        "verify": {"ok": True, "drift": {"events": 0}, "truth": {"parse_errors": 0}},
        "drill": {"error": "throwaway home failed"},
        "ok": False, "failed_stages": ["backup", "drill"],
    }
    rc = cli.report_nightly(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "backup: ERROR dest not mounted" in out
    assert "verify [shallow]: ok" in out
    assert "restore drill: ERROR throwaway home failed" in out
    assert "NIGHTLY FAILED: backup, drill" in out


def test_nightly_verify_failed_no_log(capsys) -> None:
    """Verify failed but no failure_log recorded — skips the log-pointer line."""
    res = {
        "backup": {"files_copied": 1, "bytes_copied": 0},
        "escalations": {"deep": False, "hashes": False},
        "verify": {"ok": False, "failed_components": ["fts_orphans"],
                   "drift": {"events": 0}, "truth": {"parse_errors": 0}},
        "ok": False, "failed_stages": ["verify"],
    }
    assert cli.report_nightly(res) == 1
    out = capsys.readouterr().out
    assert "verify [shallow]: FAILED (fts_orphans)" in out
    assert "full result appended" not in out


def test_nightly_verify_error_branch(capsys) -> None:
    res = {
        "backup": {"files_copied": 0, "bytes_copied": 0},
        "escalations": {"deep": False, "hashes": True},
        "verify": {"error": "index locked"},
        "ok": False, "failed_stages": ["verify"],
    }
    rc = cli.report_nightly(res)
    assert rc == 1
    out = capsys.readouterr().out
    assert "verify [hashes]: ERROR index locked" in out


# ── repair: applied (non-dry-run) branches ───────────────────────────────────


def test_repair_applied_with_samples(capsys) -> None:
    res = {
        "dry_run": False, "fragments_quarantined": 3, "files_damaged": 2,
        "damaged_sample": ["truth/threads/x.jsonl:9"], "quarantine_file": "/home/quarantine.jsonl",
        "events_restored_from_index": 4, "kg_events_restored": 1, "thread_records_restored": 2,
    }
    rc = cli.report_repair(res)
    assert rc == 0
    out = capsys.readouterr().out
    assert "quarantined 3 unparseable line(s) across 2 file(s)" in out
    assert "sample: ['truth/threads/x.jsonl:9']" in out
    assert "ledger: /home/quarantine.jsonl" in out
    assert "restored from index: 4 event(s)" in out
    assert "the repaired files shrank" in out
    assert "run `archive verify`" in out


# ── redact: false branches (no key_id / no scrubbed) + whole-thread list ──────


def test_redact_report_minimal_no_key(capsys) -> None:
    """A redact result without a key_id / scrubbed counts / notes exercises the
    skip branches of the summary block."""
    rc = cli.report_redact({"events_redacted": 1, "thread_id": 2})
    assert rc == 0
    out = capsys.readouterr().out
    assert "redacted 1 event(s) in thread 2" in out
    assert "under key" not in out
    assert "reverse with" not in out


def test_redact_report_names_scrubbed_quotes_and_the_reversal(capsys) -> None:
    """Curation quotes carrying the redacted content are scrubbed too, and the
    summary names both that and the key the redaction reverses under."""
    rc = cli.report_redact({
        "events_redacted": 2, "thread_id": 7, "key_id": "k1",
        "topic_quotes_scrubbed": 1, "kg_quotes_scrubbed": 0,
        "notes": ["provider store keeps its plaintext"],
    })
    assert rc == 0
    out = capsys.readouterr().out
    assert "redacted 2 event(s) in thread 7 under key k1" in out
    assert "scrubbed 1 topic quote(s), 0 kg quote(s)" in out
    assert "note: provider store keeps its plaintext" in out
    assert "unredact k1" in out


def test_redact_restore_key_puts_the_key_back(archive_home, tmp_path, capsys) -> None:
    """--restore-key reaches api.redact_restore_key: an escrowed key really
    returns to the keyring, and the ledger says the redaction is reversible again."""
    tid = import_cc_session(tmp_path, "escrow").thread_id
    assert main(["redact", str(tid), "--home", str(archive_home)]) == 0
    key_id = capsys.readouterr().out.split("under key ")[1].split()[0]

    assert main(["redact", "--show-key", key_id, "--home", str(archive_home)]) == 0
    key_b64 = capsys.readouterr().out.splitlines()[0]
    assert main(["redact", "--forget", key_id, "--yes", "--home", str(archive_home)]) == 0
    capsys.readouterr()

    rc = main(["redact", "--restore-key", key_id, key_b64, "--home", str(archive_home)])
    assert rc == 0
    assert f"key {key_id} restored to the keyring" in capsys.readouterr().out
    assert main(["redact", "--list", "--home", str(archive_home)]) == 0
    assert "key present" in capsys.readouterr().out


# ── unredact: notes branch ────────────────────────────────────────────────────


def test_unredact_prints_notes(archive_home, tmp_path, capsys) -> None:
    """`archive unredact` restores the events and echoes the notes the operation
    really produced — the caveats an operator must read after a restore."""
    tid = import_cc_session(tmp_path, "notes").thread_id
    assert main(["redact", str(tid), "--home", str(archive_home)]) == 0
    redacted = capsys.readouterr().out
    key_id = redacted.split("under key ")[1].split()[0]
    n_events = int(redacted.split("redacted ")[1].split()[0])

    rc = main(["unredact", key_id, "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"restored {n_events} event(s) in thread {tid}" in out
    assert "note: " in out


# ── status: fully-populated ok + failed variants ─────────────────────────────


def _status_base(**over) -> dict:
    base = {
        "home": "/h", "truth_dir": "/h/truth", "index_path": "/h/index.db",
        "threads": 5, "events": 50, "fts_indexed": 50,
    }
    base.update(over)
    return base


def test_status_all_ok(capsys) -> None:
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
    assert cli.report_status(st) == 0
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


def test_status_green_coverage_still_shows_warnings(capsys) -> None:
    # A stale export is a capture hole in the making; a green coverage check must
    # not swallow the warning that says so.
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_coverage={"ok": True, "sources_checked": 6, "at": old,
                       "warnings": ["chatgpt-export: last export 127d ago"]},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "coverage: ok (6 sources, 1 warning(s))" in out
    assert "chatgpt-export: last export 127d ago" in out


def test_status_all_failed(capsys) -> None:
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
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "verify:  FAILED (drift)" in out
    assert "backup:  FAILED → /vol/bak" in out
    assert "nightly: FAILED (backup, restore-drill) → /vol/off" in out
    assert "drill:   FAILED" in out
    assert "coverage: FAILED" in out
    assert "m1" in out and "m3" in out and "m4" not in out  # capped at 3
    assert "last pass" in out  # watch pass with no parse errors
    assert "no pass recorded" not in out


def test_status_source_mirror_ok(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_source_mirror={"ok": True, "copied": 12, "files": 340, "at": old},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "source mirror: ok (12 copied / 340 files)" in out


def test_status_source_mirror_failed_counts_errors(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_source_mirror={"ok": False, "copied": 0, "files": 5, "errors": 2, "at": old},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "source mirror: FAILED (0 copied / 5 files, 2 error(s))" in out


def test_status_self_update_applied(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_self_update={"ok": True, "action": "updated", "current": "0.9.0",
                          "reason": "updated 0.9.0 → 0.9.1", "at": old},
    )
    assert cli.report_status(st) == 0
    assert "update:  updated 0.9.0 → 0.9.1" in capsys.readouterr().out


def test_status_self_update_checked_clean(capsys) -> None:
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_self_update={"ok": True, "action": "up-to-date", "current": "0.9.1",
                          "reason": "newest tag is installed", "at": old},
    )
    assert cli.report_status(st) == 0
    assert "update:  up-to-date (v0.9.1) checked" in capsys.readouterr().out


def test_status_self_update_blocked_is_shouted(capsys) -> None:
    """A stopped update mechanism is the reason the line exists — it reads loud."""
    old = "2026-07-10T00:00:00+00:00"
    st = _status_base(
        last_self_update={"ok": False, "action": "blocked", "current": "0.9.0",
                          "reason": "truth format 4 > this install reads 3", "at": old},
    )
    assert cli.report_status(st) == 0
    out = capsys.readouterr().out
    assert "update:  BLOCKED: truth format 4 > this install reads 3" in out


# ── coverage: source states, disabled/unwatched, skips, failure ──────────────


def test_coverage_full_surface_failed(capsys) -> None:
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
    rc = cli.report_coverage(result)
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


def test_coverage_report_names_validation_drift(capsys) -> None:
    """The validation-drift ledger volume is a durable operator surface for parser
    format drift, not just a daemon log line."""
    result = {
        "ok": True, "failed": [], "warnings": [],
        "sources": {}, "disabled": {}, "unwatched": {},
        "skips": {"total": 0, "recent": 0, "recent_lines": 0, "days": 7.0},
        "drift": {"total": 3, "recent": 2, "recent_findings": 5, "days": 7.0},
    }
    assert cli.report_coverage(result) == 0
    out = capsys.readouterr().out
    assert "validation drift: 3 ledger records, 2 in last 7d (5 findings)" in out
    assert "validation-drift.jsonl" in out


def test_coverage_report_names_degraded_and_quarantined(capsys) -> None:
    """One remedy line per degraded source, and any quarantined raw-store snapshots."""
    result = {
        "ok": True, "failed": [], "warnings": [],
        "sources": {}, "disabled": {}, "unwatched": {},
        "skips": {"total": 0, "recent": 0, "recent_lines": 0, "days": 7.0},
        "drift": {"total": 0, "recent": 0, "recent_findings": 0, "days": 7.0},
        "degraded": {
            "grok": {"reason": "went_dark", "since": "2026-07-10T00:00:00Z"},
            "chatgpt": {"reason": "capture_skips", "since": None},
        },
        "drift_snapshots": {"cursor": "gen-3"},
    }
    assert cli.report_coverage(result) == 0
    out = capsys.readouterr().out
    assert "degraded: grok (went_dark since 2026-07-10) — remedy: archive fix-import grok" in out
    assert "degraded: chatgpt (capture_skips) — remedy: archive fix-import chatgpt" in out
    assert "quarantined: cursor raw store snapshot → gen-3" in out
    assert "OK" in out


# ── self-update: the four outcomes, flag mapping, exit code ──────────────────


def test_self_update_applied(monkeypatch, capsys) -> None:
    seen = {}

    def fake_update(*, home=None, check_only=False, allow_format_bump=False):
        seen.update(home=home, check_only=check_only, allow_format_bump=allow_format_bump)
        return {"ok": True, "action": "updated", "current": "0.9.0", "tag": "v0.9.1",
                "reason": "updated 0.9.0 → v0.9.1"}

    monkeypatch.setattr(_update, "self_update", fake_update)
    rc = main(["self-update", "--home", "/h", "--allow-format-bump"])
    assert rc == 0
    assert seen == {"home": "/h", "check_only": False, "allow_format_bump": True}
    assert "self-update: updated 0.9.0 → v0.9.1" in capsys.readouterr().out


def test_self_update_check_reports_available(capsys) -> None:
    """``--check`` plans only, so its output has to name the verb that applies it."""
    rc = cli.report_self_update(
        {"ok": True, "action": "update", "current": "0.9.0", "tag": "v0.9.1",
         "reason": "tag is 30h old"},
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "self-update: v0.9.1 available (tag is 30h old)" in out
    assert "run `archive self-update` to apply" in out


def test_self_update_up_to_date_lists_skipped(capsys) -> None:
    rc = cli.report_self_update(
        {"ok": True, "action": "up-to-date", "current": "0.9.1",
         "reason": "newest tag is installed",
         "skipped": ["v0.9.2: only 2h old", "v0.9.3: format bump"]},
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "self-update: up to date (v0.9.1) — newest tag is installed" in out
    assert "· v0.9.2: only 2h old" in out and "· v0.9.3: format bump" in out


def test_self_update_blocked_returns_1(capsys) -> None:
    rc = cli.report_self_update(
        {"ok": False, "action": "blocked", "current": "0.9.0",
         "reason": "truth format 4 > this install reads 3"},
    )
    assert rc == 1
    assert "self-update: BLOCKED: truth format 4 > this install reads 3" in capsys.readouterr().out


def test_self_update_actionless_result_returns_1(capsys) -> None:
    """A result with no action at all still prints and still fails — the operator
    must not read silence as success."""
    rc = cli.report_self_update({"ok": False})
    assert rc == 1
    assert "self-update: ?: None" in capsys.readouterr().out


# ── mirror: per-provider rows, extras, unsupported, exit code ────────────────


def _mirror_provider(**over) -> dict:
    p = {"ok": True, "files": 10, "copied": 2, "unchanged": 8,
         "bytes_in": 1000, "bytes_out": 400}
    p.update(over)
    return p


def test_mirror_cli_sweeps_the_real_sources(archive_home, capsys) -> None:
    """`archive mirror` runs the real raw-store sweep into <home>/source-mirror.
    Nothing is on disk to mirror in a throwaway home, so the run is green and
    the root it names is the one it created."""
    rc = main(["mirror", "--home", str(archive_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"OK → {archive_home / 'source-mirror'}" in out


def test_mirror_ok_minimal_rows(capsys) -> None:
    result = {
        "ok": True, "root": "/h/source-mirror", "duration_s": 1.5,
        "providers": {"claude-code": _mirror_provider()},
        "unsupported": [],
    }
    rc = cli.report_mirror(result)
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-code      ok       files=10 copied=2 unchanged=8 bytes=1000→400" in out
    assert "OK → /h/source-mirror (1.5s)" in out


def test_mirror_failed_provider_reports_extras_and_errors(capsys) -> None:
    result = {
        "ok": False, "root": "/h/source-mirror", "duration_s": 4.0,
        "providers": {
            "codex": _mirror_provider(generations=3, sidecars_capped=7),
            "cursor": _mirror_provider(ok=False, error_count=2,
                                       errors=["sweep: OSError: disk full",
                                               "db: locked"]),
        },
        "unsupported": ["demo-harness"],
    }
    rc = cli.report_mirror(result)
    assert rc == 1
    out = capsys.readouterr().out
    assert "generations=3" in out and "capped=7" in out
    assert "cursor" in out and "FAILED" in out and "errors=2" in out
    assert "    sweep: OSError: disk full" in out and "    db: locked" in out
    assert "demo-harness     unsupported (watcher shape has no mirror path)" in out
    assert "FAILED → /h/source-mirror (4.0s)" in out


# ── main() with no subcommand prints help ────────────────────────────────────


def test_main_no_command_prints_help(capsys) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "archive" in out
