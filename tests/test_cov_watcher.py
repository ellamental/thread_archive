"""Branch-coverage tests for the watcher daemon cluster.

Three modules, each driven through real code paths over real store layouts and
real locks, with no subprocesses:

* ``_watcher.sources`` — store discovery + the per-provider watchers. Fed store
  layouts under tmp dirs (or a redirected ``$HOME`` for the ``Path.home()``
  discoverers), exercising the discover / enable / skip branches.
* ``_watcher.exthost`` — the steering-recovery pure helpers + the ``_process_log``
  gates, over a real transcript layout under a redirected ``$HOME``.
* ``_watcher.daemon`` — the poll/maintenance/embed lifecycle. The ingest and
  reindex locks are the product's own flocks, really held (a distinct fd is a
  distinct flock owner, so one process can play both sides), and a tick-bounded
  run ends through :meth:`Watcher.stop` — the daemon's own shutdown path.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

import thread_archive._ops.health as health
import thread_archive._watcher.exthost as ex
import thread_archive._watcher.lazy as lazy
import thread_archive._watcher.paths as wpaths
import thread_archive._watcher.sources as src
from thread_archive._store import Thread, get_session, init_db
from thread_archive._truth.locks import _reindex_lock_path
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.daemon import Watcher
from thread_archive._watcher.exthost import ExthostWatcher, parse_exthost_log
from thread_archive._watcher.sources import (
    ClaudeCodeWatcher,
    CoworkWatcher,
    DbScanWatcher,
    FileSessionWatcher,
    codex_watcher,
    cursor_watcher,
    discover_claude_dirs,
    discover_claude_science_dbs,
    discover_cowork_session_dirs,
    opencode_watcher,
)

from .helpers import import_cc_session

# coverage tag: watch


# ── helpers ──────────────────────────────────────────────────────────────────


def _home(tmp_path, monkeypatch) -> Path:
    """Redirect ``$HOME`` (which ``Path.home()`` reads live) at a per-test dir so the
    ``Path.home()``-based discoverers see an isolated, empty machine."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _recv(obj: dict, ts: str = "2026-06-25 10:00:00.000") -> str:
    return f"{ts} [info] Received message from webview: {json.dumps(obj)}"

def _exthost_layout(monkeypatch, tmp_path, *, session="sess-1", content=None, as_dir=False):
    """The real Claude Code transcript layout under a redirected $HOME, so the
    real ``_session_jsonl`` glob resolves /proj + ``session`` naturally:
    ``content=None`` leaves no file at all (→ None), ``as_dir`` makes the path a
    directory (resolvable but unreadable)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-proj"
    f = proj / f"{session}.jsonl"
    if as_dir:
        f.mkdir(parents=True)
    elif content is not None:
        proj.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return f



# ══ sources.py ═══════════════════════════════════════════════════════════════


def test_discover_claude_dirs_globs_and_falls_back(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    # No .claude* dirs → the fallback single default path.
    assert discover_claude_dirs() == [home / ".claude" / "projects"]

    # Two rename-generation dirs → both discovered, sorted.
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude1" / "projects").mkdir(parents=True)
    found = discover_claude_dirs()
    assert found == [home / ".claude" / "projects", home / ".claude1" / "projects"]


def test_file_session_watcher_base_seams_raise() -> None:
    """The base class leaves ``iter_files`` / ``import_session`` unimplemented; a
    subclass that forgets them hits the NotImplementedError seams rather than
    silently watching nothing."""

    class _Bare(FileSessionWatcher):
        source_name = "bare"

        def is_available(self) -> bool:
            return True

    w = _Bare()
    with pytest.raises(NotImplementedError):
        w.iter_files()
    with pytest.raises(NotImplementedError):
        w.import_session(Path("x.jsonl"), "sid")


def test_probe_skips_unstattable_and_empty(tmp_path) -> None:
    w = ClaudeCodeWatcher(projects_dirs=[tmp_path])
    # Missing file → stat raises → silent skip (no fingerprint retained).
    assert w._probe((tmp_path / "gone.jsonl", "sid")) is None
    # Empty file (mid-create) → skipped until it has content.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert w._probe((empty, "sid")) is None


def test_claude_code_dirs_default_to_discovery(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    (home / ".claude" / "projects").mkdir(parents=True)
    # No explicit projects_dirs → resolves via discover_claude_dirs().
    assert ClaudeCodeWatcher()._dirs() == [home / ".claude" / "projects"]


def test_claude_code_iter_files_skips_and_finds_subagents(tmp_path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "loose-file").write_text("not a dir", encoding="utf-8")  # non-dir entry
    proj = projects / "proj"
    proj.mkdir()
    (proj / "sess.jsonl").write_text("{}\n", encoding="utf-8")
    agent = proj / "agent"
    (agent / "subagents").mkdir(parents=True)
    (agent / "subagents" / "agent-a.jsonl").write_text("{}\n", encoding="utf-8")
    # A workflow run nests its agents deeper; these are transcripts like any
    # other and a flat subagents glob misses every one of them.
    nested = agent / "subagents" / "workflows" / "wf_1"
    nested.mkdir(parents=True)
    (nested / "agent-b.jsonl").write_text("{}\n", encoding="utf-8")
    # …alongside a run ledger that is not a transcript. Its name repeats once
    # per workflow run, so importing it would collapse every run in a project
    # onto a single source_id.
    (nested / "journal.jsonl").write_text("{}\n", encoding="utf-8")

    missing = tmp_path / "missing-projects"  # a projects dir that doesn't exist
    w = ClaudeCodeWatcher(projects_dirs=[missing, projects])
    names = {p.name for p, _ in w.iter_files()}
    assert names == {"sess.jsonl", "agent-a.jsonl", "agent-b.jsonl"}
    # source_id carries the project-dir name prefix, and the stem alone
    # identifies a nested agent — depth never reaches the id.
    ids = {sid for _, sid in w.iter_files()}
    assert "proj:sess" in ids and "proj:agent-a" in ids and "proj:agent-b" in ids


def test_claude_code_iter_files_tolerates_unstattable_in_sort(tmp_path) -> None:
    """The oldest-first sort keys on mtime; a broken symlink can't be stat'd, so
    its key defaults to 0.0 rather than raising out of the sort."""
    projects = tmp_path / "projects"
    proj = projects / "proj"
    proj.mkdir(parents=True)
    (proj / "real.jsonl").write_text("{}\n", encoding="utf-8")
    os.symlink(proj / "nonexistent-target", proj / "broken.jsonl")

    w = ClaudeCodeWatcher(projects_dirs=[projects])
    names = sorted(p.name for p, _ in w.iter_files())
    assert names == ["broken.jsonl", "real.jsonl"]  # both yielded, sort survived


def test_rglob_watcher_skips_dirs_and_missing_root(tmp_path) -> None:
    missing = codex_watcher(sessions_dir=tmp_path / "no-codex")
    assert list(missing.iter_files()) == []  # root absent → empty

    root = tmp_path / "codex"
    root.mkdir()
    (root / "dir.jsonl").mkdir()  # matches *.jsonl glob but is a directory
    (root / "real.jsonl").write_text("{}\n", encoding="utf-8")
    w = codex_watcher(sessions_dir=root)
    files = list(w.iter_files())
    assert [p.name for p, _ in files] == ["real.jsonl"]  # the dir was skipped


def test_db_scan_current_mtime_none_for_absent(tmp_path) -> None:
    w = cursor_watcher(db_path=tmp_path / "state.vscdb")
    assert w._current_mtime(None) is None
    assert w._current_mtime(tmp_path / "gone.vscdb") is None


def test_db_scan_current_mtime_and_discover_include_wal(tmp_path) -> None:
    db = tmp_path / "opencode.db"
    db.write_bytes(b"main")
    wal = tmp_path / "opencode.db-wal"
    wal.write_bytes(b"wal")
    os.utime(wal, (2_000_000_000, 2_000_000_000))  # WAL newer than the main file

    w = opencode_watcher(db_path=db)  # watch_wal=True
    # The fingerprint is the max of (db, -wal) mtimes → the WAL's newer stamp.
    assert w._current_mtime(db) == pytest.approx(2_000_000_000, abs=1)

    d = w.discover()
    assert d.available is True
    assert d.items is None  # DB sources don't count conversations
    assert d.latest == pytest.approx(2_000_000_000, abs=1)  # WAL activity surfaced


def test_db_scan_discover_wal_loop_tolerates_stat_error(tmp_path) -> None:
    db = tmp_path / "opencode.db"
    db.write_bytes(b"main")
    w = opencode_watcher(db_path=db)

    def _raise(_p):
        raise OSError("stat failed")

    w._current_mtime = _raise  # WAL-activity probe raises → caught, loop continues
    d = w.discover()
    assert isinstance(d, SourceDiscovery)  # no exception escaped


def test_db_scan_discover_wal_loop_skips_none_mtime(tmp_path) -> None:
    # Missing DB, but watch_wal=True → the WAL loop still runs and gets a None mtime.
    w = opencode_watcher(db_path=tmp_path / "absent.db")
    d = w.discover()
    assert d.available is False
    assert d.latest is None  # None mtime → the latest-update branch was skipped


def test_db_scan_probe_reports_stat_failure(tmp_path) -> None:
    db = tmp_path / "state.vscdb"
    db.write_bytes(b"x")
    w = cursor_watcher(db_path=db)

    def _raise(_p):
        raise OSError("permission denied")

    w._current_mtime = _raise  # instance override (mirrors test_watch.py's style)
    res = w._probe((db, lambda: None, "cursor"))
    assert isinstance(res, WatchResult)
    assert res.errors and "cannot stat db" in res.errors[0]


def test_db_scan_probe_reports_missing_db(tmp_path) -> None:
    gone = tmp_path / "gone.vscdb"
    w = cursor_watcher(db_path=gone)
    res = w._probe((gone, lambda: None, "cursor"))
    assert isinstance(res, WatchResult)
    assert res.errors and "database not found" in res.errors[0]


def test_db_scan_poll_routes_scanner_raise_to_error(tmp_path) -> None:
    db = tmp_path / "boom.db"
    db.write_bytes(b"x")

    def boom(_p):
        raise RuntimeError("scan blew up")

    w = DbScanWatcher(db, "boomsrc", boom)
    res = w.poll()
    assert res.errors and "scan failed" in res.errors[0]
    assert "scan blew up" in res.errors[0]
    # The fingerprint did NOT advance — the failed scan retries next poll.
    assert res.events_created == 0


def _force_system(monkeypatch, name: str) -> None:
    """The one seam this file cannot inject through: the per-OS store default is a
    live ``platform.system()`` read inside ``app_data_dir``, and every OS's branch
    has to be provable from whichever host runs the suite. Patches the shared
    ``platform`` singleton, so every reader (cursor, cowork, exthost) sees it."""
    monkeypatch.setattr(wpaths.platform, "system", lambda: name)


def test_app_data_dir_platform_branches(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)

    _force_system(monkeypatch, "Darwin")
    assert wpaths.app_data_dir() == home / "Library" / "Application Support"

    _force_system(monkeypatch, "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert wpaths.app_data_dir() == home / ".config"
    # An absolute XDG_CONFIG_HOME outranks $HOME; a relative one is ignored (per spec).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert wpaths.app_data_dir() == tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/nope")
    assert wpaths.app_data_dir() == home / ".config"

    _force_system(monkeypatch, "Windows")
    monkeypatch.delenv("APPDATA", raising=False)
    assert wpaths.app_data_dir() is None
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert wpaths.app_data_dir() == tmp_path / "appdata"

    _force_system(monkeypatch, "Plan9")  # unknown OS → no known root
    assert wpaths.app_data_dir() is None


def test_cursor_default_db_platform_branches(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)

    _force_system(monkeypatch, "Linux")
    assert src._cursor_default_db() is None  # constructed path doesn't exist

    _force_system(monkeypatch, "Windows")
    monkeypatch.setenv("APPDATA", "")  # empty APPDATA → give up
    assert src._cursor_default_db() is None
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))  # set but missing file
    assert src._cursor_default_db() is None

    _force_system(monkeypatch, "Plan9")  # unknown OS
    assert src._cursor_default_db() is None

    # Darwin, and the file actually present → returns it.
    _force_system(monkeypatch, "Darwin")
    assert src._cursor_default_db() is None  # not created yet
    dbpath = (home / "Library" / "Application Support" / "Cursor" / "User"
              / "globalStorage" / "state.vscdb")
    dbpath.parent.mkdir(parents=True)
    dbpath.write_bytes(b"x")
    assert src._cursor_default_db() == dbpath


def test_discover_cowork_session_dirs(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    # Prove the Darwin branch on any host: fixtures live at the macOS app-data
    # path, so the live platform.system() read inside app_data_dir must see Darwin
    # (as the cursor/app-data tests above do), else a Linux host resolves ~/.config
    # and discovers nothing.
    _force_system(monkeypatch, "Darwin")
    # Base absent → empty.
    assert discover_cowork_session_dirs() == []

    base = home / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    (base / "user1" / "orgA").mkdir(parents=True)
    (base / "user1" / "orgB").mkdir(parents=True)
    (base / "skills-plugin").mkdir()  # reserved name → skipped
    (base / "loose").write_text("x", encoding="utf-8")  # user-level non-dir → skipped
    (base / "user1" / "notadir").write_text("y", encoding="utf-8")  # org-level non-dir

    dirs = discover_cowork_session_dirs()
    assert {d.name for d in dirs} == {"orgA", "orgB"}
    assert all(d.parent.name == "user1" for d in dirs)


def test_cowork_watcher_end_to_end(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    # Fixtures at the macOS app-data path → force the Darwin branch so the watcher
    # discovers them on a Linux host too (see test_discover_cowork_session_dirs).
    _force_system(monkeypatch, "Darwin")
    org_dir = (home / "Library" / "Application Support" / "Claude"
               / "local-agent-mode-sessions" / "user1" / "orgA")
    org_dir.mkdir(parents=True)

    # A well-formed session, a non-local dir, and a local dir without audit.jsonl.
    sess = org_dir / "local_sess1"
    sess.mkdir()
    (sess / "audit.jsonl").write_text("\n".join(json.dumps(ln) for ln in [
        {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
         "sessionId": "s1", "cwd": "/proj",
         "message": {"role": "user", "content": "cowork task"}},
        {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": "working on it"}]}},
    ]) + "\n", encoding="utf-8")
    (org_dir / "local_sess1.json").write_text(
        json.dumps({"title": "My Cowork Task"}), encoding="utf-8"
    )
    (org_dir / "not-a-session").mkdir()  # doesn't start with local_ → skipped
    (org_dir / "local_empty").mkdir()  # local_ dir but no audit.jsonl → skipped

    init_db()
    w = CoworkWatcher()
    assert w.is_available() is True
    r = w.poll()
    assert r.items_imported == 1 and r.events_created > 0
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "cowork")).scalar_one()
        assert t.title == "My Cowork Task"
        assert t.source_id == "user1:orgA:sess1"
    # The metadata path was remembered per source_id at iteration time.
    assert w._metadata["user1:orgA:sess1"] == org_dir / "local_sess1.json"


def test_discover_claude_science_dbs(tmp_path) -> None:
    # Absent base → empty.
    assert discover_claude_science_dbs(tmp_path / "no-orgs") == []

    base = tmp_path / "orgs"
    (base / "org1").mkdir(parents=True)
    (base / "org1" / "operon-cli.db").write_bytes(b"x")
    (base / "org2").mkdir()  # dir but no operon-cli.db → excluded
    (base / "loose").write_text("x", encoding="utf-8")  # non-dir → skipped

    out = discover_claude_science_dbs(base)
    assert out == [(base / "org1" / "operon-cli.db", "org1")]


# ══ exthost.py ═══════════════════════════════════════════════════════════════


def test_too_fresh_grace_window() -> None:
    from datetime import datetime, timedelta

    assert ex._too_fresh(None) is False  # absent stamp → act now
    fresh = datetime.now().replace(microsecond=0).isoformat()
    assert ex._too_fresh(fresh) is True  # just now → within grace
    old = (datetime.now() - timedelta(seconds=ex.GRACE_SECONDS + 60)).isoformat()
    assert ex._too_fresh(old) is False  # aged out → act now


def test_content_text_variants() -> None:
    assert ex._content_text("plain") == "plain"
    assert ex._content_text([{"type": "text", "text": "a"},
                             {"type": "image"},
                             {"type": "text", "text": "b"}]) == "ab"
    assert ex._content_text([]) is None  # list with no text blocks
    assert ex._content_text(123) is None  # neither str nor list


def test_session_jsonl_resolution(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    proj = home / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    f = proj / "sess-123.jsonl"
    f.write_text("{}\n", encoding="utf-8")

    assert ex._session_jsonl("/proj", "sess-123") == f  # found on disk
    assert ex._session_jsonl("/proj", "other-session") is None  # not present


def test_parse_exthost_log_branches() -> None:
    lines = [
        "a plain line with no webview marker",  # no _RECV match → skipped
        "prefix Received message from webview: {not valid json",  # json error → skipped
        # control line: carries channel cwd + resumed session id
        _recv({"channelId": "c1", "cwd": "/proj", "resume": "sess-9",
               "message": {"type": "request"}}),
        # user message with NO channelId → control block skipped AND dropped (no ch)
        _recv({"message": {"type": "user", "uuid": "no-chan",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "x"}]}}}),
        # user message whose content yields no text → dropped
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "empty-content",
                           "message": {"role": "user", "content": 123}}}),
        # a genuine, capturable user message
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "good",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "steer"}]}}}),
        # already handled this process → filtered by skip_uuids
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "already",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "dup"}]}}}),
        # a bare slash-command → not a loss
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "cmd",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "/debrief"}]}}}),
    ]
    chan_cwd, chan_session, messages, bare = parse_exthost_log(
        "\n".join(lines), skip_uuids={"already"}
    )
    assert chan_cwd == {"c1": "/proj"}
    assert chan_session == {"c1": "sess-9"}
    uuids = [u for _, u, _, _ in messages]
    assert uuids == ["good"]  # only the one capturable message survives every gate
    assert bare == ["cmd"]


def test_parse_exthost_log_captures_timestamp() -> None:
    line = _recv({"channelId": "c1",
                  "message": {"type": "user", "uuid": "u",
                              "message": {"role": "user",
                                          "content": [{"type": "text", "text": "hi"}]}}},
                 ts="2026-06-25 14:59:13.302")
    _, _, messages, _ = parse_exthost_log(line)
    assert messages[0][3] == "2026-06-25T14:59:13.302"  # space→T normalized


def test_exthost_poll_skips_unstattable_and_empty_logs(tmp_path) -> None:
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")  # size 0 → skipped
    w = ExthostWatcher(log_globs=[str(empty)])
    assert w.poll() == WatchResult()  # nothing folded, nothing fingerprinted
    assert str(empty) not in w._seen_fp

    # A log path that resolves but can't be stat'd → skipped without raising.
    w2 = ExthostWatcher(log_globs=[])
    w2._logs = lambda: ["/no/such/dir/ghost.log"]
    assert w2.poll() == WatchResult()


def test_exthost_poll_survives_one_bad_log(tmp_path, monkeypatch) -> None:
    log = tmp_path / "Claude VSCode.log"
    log.write_text(_recv({"channelId": "c1"}) + "\n", encoding="utf-8")
    w = ExthostWatcher(log_globs=[str(log)])

    def boom(_p):
        raise RuntimeError("parse exploded")

    w._process_log = boom
    res = w.poll()
    assert res.sources_checked == 1
    assert res.errors and "cc-exthost" in res.errors[0]
    assert "parse exploded" in res.errors[0]


def test_exthost_process_log_skips_channel_without_context(archive_home, tmp_path, monkeypatch) -> None:
    """A user message on a channel with no control line (no cwd/session) is left
    for a later poll rather than misfiled."""
    init_db()
    log = tmp_path / "Claude VSCode.log"
    # user message, but NO control line establishing the channel's cwd/session
    log.write_text(_recv({"channelId": "c1",
                          "message": {"type": "user", "uuid": "orphan",
                                      "message": {"role": "user",
                                                  "content": [{"type": "text",
                                                               "text": "hi"}]}}}) + "\n",
                   encoding="utf-8")
    r = ExthostWatcher(log_globs=[str(log)]).poll()
    assert r.events_created == 0


def test_exthost_process_log_skips_when_no_jsonl(archive_home, tmp_path, monkeypatch) -> None:
    """No session JSONL on disk → can't confirm lost-vs-persisted → leave it."""
    init_db()
    _exthost_layout(monkeypatch, tmp_path)  # no transcript on disk at all
    log = tmp_path / "Claude VSCode.log"
    log.write_text("\n".join([
        _recv({"channelId": "c1", "cwd": "/proj", "resume": "sess-1",
               "message": {"type": "request"}}),
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "lost",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "hi"}]}}}),
    ]) + "\n", encoding="utf-8")
    r = ExthostWatcher(log_globs=[str(log)]).poll()
    assert r.events_created == 0


def test_exthost_process_log_tolerates_unreadable_jsonl(archive_home, tmp_path, monkeypatch) -> None:
    """If the resolved session JSONL can't be read (here: it's a directory), the
    read is caught (cache the empty string) and the message still gets considered."""
    init_db()
    _exthost_layout(monkeypatch, tmp_path, as_dir=True)  # resolvable, unreadable
    log = tmp_path / "Claude VSCode.log"
    log.write_text("\n".join([
        _recv({"channelId": "c1", "cwd": "/proj", "resume": "sess-1",
               "message": {"type": "request"}}),
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "lost",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "hi"}]}}}),
    ]) + "\n", encoding="utf-8")
    # No thread exists for the session, so after the unreadable-jsonl branch the
    # message still resolves to no thread and is left — the point is the read
    # didn't raise out of the poll.
    r = ExthostWatcher(log_globs=[str(log)]).poll()
    assert r.events_created == 0


def test_exthost_recovers_two_lost_messages_sharing_thread(archive_home, tmp_path, monkeypatch) -> None:
    """Two lost steering messages on the same channel/session import into the one
    thread — the second reuses the resolved thread from the per-poll cache."""
    from thread_archive._importers._state import create_thread
    from thread_archive._store import Event

    init_db()
    source_id = "-proj:sess-1"
    with get_session() as s:
        tid = create_thread(s, source="claude-code", source_id=source_id, title="t")
        s.commit()

    _exthost_layout(monkeypatch, tmp_path, content="")  # empty → neither uuid present

    log = tmp_path / "Claude VSCode.log"
    log.write_text("\n".join([
        _recv({"channelId": "c1", "cwd": "/proj", "resume": "sess-1",
               "message": {"type": "request"}}),
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "lost-1",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "first steer"}]}}}),
        _recv({"channelId": "c1",
               "message": {"type": "user", "uuid": "lost-2",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": "second steer"}]}}}),
    ]) + "\n", encoding="utf-8")

    r = ExthostWatcher(log_globs=[str(log)]).poll()
    assert r.events_created == 2
    with get_session() as s:
        texts = [e.payload.get("content") for e in
                 s.execute(select(Event).where(Event.thread_id == tid)).scalars()]
    assert "first steer" in texts and "second steer" in texts


# ══ daemon.py ════════════════════════════════════════════════════════════════


class _StubSource(SourceWatcher):
    """A minimal watcher whose poll yield is configurable per instance.

    ``on_poll`` is the loop's own stop button: a source that calls
    ``Watcher.stop`` when polled ends the run after exactly one tick, through the
    product's public stop path rather than a faked lock.
    """

    def __init__(self, name: str = "stub", *, available: bool = True, events: int = 0) -> None:
        self._name = name
        self._available = available
        self._events = events
        self.polls = 0
        self.on_poll: Optional[Callable[[], None]] = None

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def poll(self) -> WatchResult:
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll()
        return WatchResult(sources_checked=1, events_created=self._events)


@contextmanager
def _reindex_holds_the_lock():
    """A real exclusive hold on ``<home>/.reindex.lock`` — what ``thread-archive index rebuild``
    takes across its build-and-swap. A distinct fd is a distinct flock owner even
    in one process, so the loop's shared non-blocking acquire genuinely fails
    against it."""
    path = _reindex_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextmanager
def _ingest_owner_held():
    """A real exclusive hold on ``<home>/.ingest-owner.lock`` — the lock a live
    daemon (or a lazy catch-up pass mid-flight) keeps, so ``acquire_ingest_owner``
    really comes back empty."""
    path = lazy._lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _wait_until(predicate, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


@contextmanager
def _loop_running(w: Watcher):
    """Run ``_run_loop`` on a thread for as long as the block needs it, then stop
    it through :meth:`Watcher.stop` and join — the daemon's real shutdown."""
    thread = threading.Thread(target=w._run_loop, name="watch-loop", daemon=True)
    thread.start()
    try:
        yield
    finally:
        w.stop()
        thread.join(20)
    assert not thread.is_alive(), "the loop did not stop"


def test_poll_once_skips_unavailable_sources(archive_home) -> None:
    avail = _StubSource("up", available=True, events=1)
    absent = _StubSource("down", available=False, events=99)
    w = Watcher([avail, absent], embed=False)
    total = w.poll_once()
    assert avail.polls == 1
    assert absent.polls == 0  # is_available() False → skipped before poll
    assert total.events_created == 1


def _break_health_recording(monkeypatch) -> None:
    """The loop's health writes, forced to fail *past* their own guard.

    ``record_health`` already swallows every OSError it can hit (an unwritable
    home, a lock it cannot open), so the belt-and-braces catch in the loop —
    which exists so no future recording bug can take the daemon down — has no
    reachable real trigger. This is the one seam here with nothing behind the
    front door to inject.
    """

    def boom(*a, **k):
        raise RuntimeError("health write failed")

    monkeypatch.setattr(health, "record_health", boom)


def test_record_errors_throttles_then_exceptions_are_swallowed(archive_home, monkeypatch, caplog) -> None:
    w = Watcher([], embed=False)
    # First call records; a second within the 60s window is throttled (early return).
    w._record_errors(["e1"])
    assert w._errors_recorded_at is not None
    stamp = w._errors_recorded_at
    w._record_errors(["e2"])
    assert w._errors_recorded_at == stamp  # unchanged → the throttle branch ran
    assert w._errors_total == 2  # but the running total still climbed

    # Recording failures are advisory — swallowed, never raised.
    fresh = Watcher([], embed=False)

    _break_health_recording(monkeypatch)
    with caplog.at_level("ERROR", logger="thread_archive._watcher.daemon"):
        fresh._record_errors(["e"])  # must not raise
    assert any("could not record poll errors" in r.getMessage() for r in caplog.records)


def test_record_pass_swallows_recording_failure(archive_home, monkeypatch, caplog) -> None:
    w = Watcher([], embed=False)

    _break_health_recording(monkeypatch)
    with caplog.at_level("ERROR", logger="thread_archive._watcher.daemon"):
        w._record_pass()  # advisory — must not raise
    assert any("could not record pass heartbeat" in r.getMessage() for r in caplog.records)


def test_clean_pass_clears_stale_watch_errors(archive_home) -> None:
    """A prior run's watch_errors_last persists in health.json across restarts.
    The first clean pass of a fresh daemon must retire it, so `thread-archive status`
    doesn't report a red the daemon already ran past."""
    from thread_archive import _api as ta

    health.record_health("watch_errors_last", {"count_since_start": 3, "errors": ["old"]})
    assert ta.status()["last_watch_errors"] is not None

    w = Watcher([], embed=False)  # fresh run: no errors this process
    w._record_pass()
    assert ta.status()["last_watch_errors"] is None
    assert w._stale_errors_cleared is True


def test_errored_run_keeps_its_watch_errors(archive_home) -> None:
    """Clear-on-green must not wipe a record the *current* run wrote: once this
    run has errored, watch_errors_last is current, not stale."""
    from thread_archive import _api as ta
    from thread_archive._watcher.base import SourceWatcher

    class Broken(SourceWatcher):
        source_name = "broken"

        def poll(self):
            raise RuntimeError("boom")

        def is_available(self):
            return True

    w = Watcher([Broken()], embed=False)
    w.poll_once()  # records a fresh error and a pass in the same tick
    rec = ta.status()["last_watch_errors"]
    assert rec is not None and rec["count_since_start"] == 1


def _seed_one_thread(tmp_path) -> None:
    """A real imported claude-code session — a thread with a title, and events for
    its search docs to anchor on."""
    import_cc_session(tmp_path)


def test_maintain_folds_meta_docs_when_present(archive_home, tmp_path) -> None:
    """A freshly imported thread's title hasn't reached the search docs yet, so
    the upkeep pass syncs it and folds the row count into its report."""
    _seed_one_thread(tmp_path)
    counts = Watcher([], embed=False).maintain()
    assert counts.get("thread_meta_docs", 0) > 0


def test_maintain_skips_meta_docs_when_empty(archive_home, tmp_path) -> None:
    """The sync is diff-based: a second pass over an unchanged archive writes
    nothing, and a falsy count is not folded into the report."""
    _seed_one_thread(tmp_path)
    w = Watcher([], embed=False)
    assert w.maintain().get("thread_meta_docs", 0) > 0
    assert "thread_meta_docs" not in w.maintain()


def test_maintain_survives_meta_index_error(archive_home, tmp_path, caplog) -> None:
    """A search surface the meta sync can no longer read (here: its shadow table
    gone) must not take the upkeep pass — or the loop behind it — down."""
    _seed_one_thread(tmp_path)
    with get_session() as s:
        s.execute(sa_text("DROP TABLE events_fts"))
        s.commit()

    with caplog.at_level("WARNING", logger="thread_archive._watcher.daemon"):
        counts = Watcher([], embed=False).maintain()  # must not raise
    assert isinstance(counts, dict)
    assert "thread_meta_docs" not in counts
    assert any("thread-meta index error" in r.getMessage() for r in caplog.records)


def test_embed_pending_returns_zero_when_caught_up(archive_home, tmp_path) -> None:
    """The cohost reports a clean zero rather than raising when the incremental
    embedder has nothing to hand back — which is every run without the
    ``[embeddings]`` extra, and every run that is already caught up."""
    _seed_one_thread(tmp_path)
    assert Watcher([], embed_batch=64).embed_pending() == 0


def test_run_warns_when_owner_lock_unavailable(archive_home, caplog) -> None:
    """When another process already holds the ingest-owner lock, ``run`` logs and
    proceeds rather than dying (which would feed launchd's restart throttle)."""
    w = Watcher([], embed=False)
    ran = {"n": 0}
    w._run_loop = lambda: ran.__setitem__("n", ran["n"] + 1)
    with _ingest_owner_held(), caplog.at_level("WARNING",
                                               logger="thread_archive._watcher.daemon"):
        w.run()  # owner_fd is None → the finally must not try to close a fd
    assert ran["n"] == 1
    assert w._owner_fd is None
    assert any("another process holds the ingest-owner lock" in r.getMessage()
               for r in caplog.records)


def test_run_loop_skips_pass_while_reindex_holds_lock(archive_home, caplog) -> None:
    """A reindex holding the lock exclusive quiesces ingest: every pass is skipped
    whole, and no source is polled while the rebuild-and-swap runs."""
    w = Watcher([_StubSource("s")], interval=0.01, embed=False)
    with caplog.at_level("INFO", logger="thread_archive._watcher.daemon"):
        with _reindex_holds_the_lock(), _loop_running(w):
            _wait_until(lambda: any("reindex in progress" in r.getMessage()
                                    for r in caplog.records))
    assert w.watchers[0].polls == 0  # lock not acquired → poll skipped


def test_run_loop_full_tick_runs_maintenance_and_embed(archive_home) -> None:
    """One acquired tick over the real ingest lock: an importing poll marks the
    loop dirty, the (due) maintenance pass runs, and the embed cohost drains — a
    filled batch flags a backlog for the next cycle. The tick then stops the loop."""
    stub = _StubSource("s", events=1)
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed_interval=0.0,
                embed=True, embed_batch=1)
    w._import_state_stamp = lambda: (1, "stamp")

    calls = {"maintain": 0, "embed": 0}

    def fake_maintain():
        calls["maintain"] += 1
        return {}

    def fake_embed():
        calls["embed"] += 1
        w._stop = True  # end the loop after the first full tick
        return 1  # == embed_batch → a filled batch signals a backlog

    w.maintain = fake_maintain
    w.embed_pending = fake_embed

    w._run_loop()

    assert stub.polls == 1  # the pass ran
    assert calls["maintain"] == 1  # dirty + due → maintenance ran
    assert calls["embed"] == 1  # embed cohost ran
    assert w._backlog is True and w._embed_more is True  # filled batch → drain next cycle


def test_run_loop_maintenance_due_but_nothing_changed_skips_it(archive_home) -> None:
    """Maintenance interval elapsed, but the poll imported nothing (not dirty) and
    no watermark moved (stamp None) → the maintenance pass is skipped."""
    stub = _StubSource("s", events=0)  # nothing imported → stays clean
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed=False)
    stub.on_poll = w.stop  # exactly one tick, through the daemon's own stop path
    calls = {"maintain": 0}
    w.maintain = lambda: calls.__setitem__("maintain", calls["maintain"] + 1) or {}
    w._import_state_stamp = lambda: None  # no watermark → maintenance condition False

    w._run_loop()

    assert stub.polls == 1
    assert calls["maintain"] == 0  # due, but nothing changed → skipped


def test_run_loop_survives_maintenance_and_embed_errors(archive_home, caplog) -> None:
    """Maintenance and embed both raise on the tick; each is caught so the loop
    survives (a raise here would exit the process into launchd's restart churn)."""
    stub = _StubSource("s", events=1)
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed_interval=0.0,
                embed=True, embed_batch=8)
    w._import_state_stamp = lambda: None  # dirty alone drives maintenance

    def bad_maintain():
        raise RuntimeError("maintenance boom")

    def bad_embed():
        w._stop = True  # stop before we raise, so the loop ends after this tick
        raise RuntimeError("embed boom")

    w.maintain = bad_maintain
    w.embed_pending = bad_embed

    with caplog.at_level("WARNING", logger="thread_archive._watcher.daemon"):
        w._run_loop()  # neither error escapes

    msgs = [r.getMessage() for r in caplog.records]
    assert any("maintenance error" in m for m in msgs)
    assert any("embed error" in m for m in msgs)
    assert w._backlog is False and w._embed_more is False  # embed failure quiesced
