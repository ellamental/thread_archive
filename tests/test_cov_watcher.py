"""Branch-coverage tests for the watcher daemon cluster.

Three modules, each driven through real code paths with no real filesystem
watching, no threads (bar one bounded, self-stopping loop run), and no
subprocesses:

* ``_watcher.sources`` — store discovery + the per-provider watchers. Fed fake
  store layouts under tmp dirs (or a redirected ``$HOME`` for the ``Path.home()``
  discoverers), exercising the discover / enable / skip branches.
* ``_watcher.exthost`` — the steering-recovery pure helpers + the ``_process_log``
  gates, with the JSONL-lookup and freshness seams stubbed so a single poll runs
  deterministically.
* ``_watcher.daemon`` — the poll/maintenance/embed lifecycle, with the ingest
  lock, the maintenance pass, and the embed cohost stubbed so one loop tick runs
  and then stops.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

import thread_archive._ops.health as health
import thread_archive._retrieval.fts as fts
import thread_archive._retrieval.vectors as vectors
import thread_archive._truth as truth
import thread_archive._watcher.exthost as ex
import thread_archive._watcher.lazy as lazy
import thread_archive._watcher.sources as src
from thread_archive._store import Thread, get_session, init_db
from thread_archive._watcher.base import SourceDiscovery, SourceWatcher, WatchResult
from thread_archive._watcher.daemon import Watcher
from thread_archive._watcher.exthost import ExthostWatcher, parse_exthost_log
from thread_archive._watcher.sources import (
    ClaudeCodeWatcher,
    CoworkWatcher,
    FileSessionWatcher,
    _DbScanWatcher,
    codex_watcher,
    cursor_watcher,
    discover_claude_dirs,
    discover_claude_science_dbs,
    discover_cowork_session_dirs,
    opencode_watcher,
)

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
    """The base class leaves ``_iter_files`` / ``_import`` unimplemented; a subclass
    that forgets them hits the NotImplementedError seams."""

    class _Bare(FileSessionWatcher):
        source_name = "bare"

        def is_available(self) -> bool:
            return True

    w = _Bare()
    with pytest.raises(NotImplementedError):
        w._iter_files()
    with pytest.raises(NotImplementedError):
        w._import(Path("x.jsonl"), "sid")


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
    (agent / "subagents" / "a.jsonl").write_text("{}\n", encoding="utf-8")

    missing = tmp_path / "missing-projects"  # a projects dir that doesn't exist
    w = ClaudeCodeWatcher(projects_dirs=[missing, projects])
    names = {p.name for p, _ in w._iter_files()}
    assert names == {"sess.jsonl", "a.jsonl"}
    # source_id carries the project-dir name prefix.
    ids = {sid for _, sid in w._iter_files()}
    assert "proj:sess" in ids and "proj:a" in ids


def test_claude_code_iter_files_tolerates_unstattable_in_sort(tmp_path) -> None:
    """The oldest-first sort keys on mtime; a broken symlink can't be stat'd, so
    its key defaults to 0.0 rather than raising out of the sort."""
    projects = tmp_path / "projects"
    proj = projects / "proj"
    proj.mkdir(parents=True)
    (proj / "real.jsonl").write_text("{}\n", encoding="utf-8")
    os.symlink(proj / "nonexistent-target", proj / "broken.jsonl")

    w = ClaudeCodeWatcher(projects_dirs=[projects])
    names = sorted(p.name for p, _ in w._iter_files())
    assert names == ["broken.jsonl", "real.jsonl"]  # both yielded, sort survived


def test_rglob_watcher_skips_dirs_and_missing_root(tmp_path) -> None:
    missing = codex_watcher(sessions_dir=tmp_path / "no-codex")
    assert list(missing._iter_files()) == []  # root absent → empty

    root = tmp_path / "codex"
    root.mkdir()
    (root / "dir.jsonl").mkdir()  # matches *.jsonl glob but is a directory
    (root / "real.jsonl").write_text("{}\n", encoding="utf-8")
    w = codex_watcher(sessions_dir=root)
    files = list(w._iter_files())
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

    w = _DbScanWatcher(db, "boomsrc", boom)
    res = w.poll()
    assert res.errors and "scan failed" in res.errors[0]
    assert "scan blew up" in res.errors[0]
    # The fingerprint did NOT advance — the failed scan retries next poll.
    assert res.events_created == 0


def test_cursor_default_db_platform_branches(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)

    monkeypatch.setattr(src.platform, "system", lambda: "Linux")
    assert src._cursor_default_db() is None  # constructed path doesn't exist

    monkeypatch.setattr(src.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", "")  # empty APPDATA → give up
    assert src._cursor_default_db() is None
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))  # set but missing file
    assert src._cursor_default_db() is None

    monkeypatch.setattr(src.platform, "system", lambda: "Plan9")  # unknown OS
    assert src._cursor_default_db() is None

    # Darwin, and the file actually present → returns it.
    monkeypatch.setattr(src.platform, "system", lambda: "Darwin")
    assert src._cursor_default_db() is None  # not created yet
    dbpath = (home / "Library" / "Application Support" / "Cursor" / "User"
              / "globalStorage" / "state.vscdb")
    dbpath.parent.mkdir(parents=True)
    dbpath.write_bytes(b"x")
    assert src._cursor_default_db() == dbpath


def test_discover_cowork_session_dirs(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
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
    old = (datetime.now() - timedelta(seconds=ex._GRACE_SECONDS + 60)).isoformat()
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
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)
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
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: None)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)
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
    jdir = tmp_path / "jsonl-is-a-dir"
    jdir.mkdir()
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jdir)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)
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

    jsonl = tmp_path / "sess-1.jsonl"
    jsonl.write_text("", encoding="utf-8")  # exists but empty → neither uuid present
    monkeypatch.setattr(ex, "_session_jsonl", lambda cwd, sid: jsonl)
    monkeypatch.setattr(ex, "_too_fresh", lambda iso: False)

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
    """A minimal watcher whose poll yield is configurable per instance."""

    def __init__(self, name: str = "stub", *, available: bool = True, events: int = 0) -> None:
        self._name = name
        self._available = available
        self._events = events
        self.polls = 0

    @property
    def source_name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def poll(self) -> WatchResult:
        self.polls += 1
        return WatchResult(sources_checked=1, events_created=self._events)


def test_poll_once_skips_unavailable_sources(archive_home) -> None:
    avail = _StubSource("up", available=True, events=1)
    absent = _StubSource("down", available=False, events=99)
    w = Watcher([avail, absent], embed=False)
    total = w.poll_once()
    assert avail.polls == 1
    assert absent.polls == 0  # is_available() False → skipped before poll
    assert total.events_created == 1


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

    def boom(*a, **k):
        raise RuntimeError("health write failed")

    monkeypatch.setattr(health, "record_health", boom)
    with caplog.at_level("ERROR", logger="thread_archive._watcher.daemon"):
        fresh._record_errors(["e"])  # must not raise
    assert any("could not record poll errors" in r.getMessage() for r in caplog.records)


def test_record_pass_swallows_recording_failure(archive_home, monkeypatch, caplog) -> None:
    w = Watcher([], embed=False)

    def boom(*a, **k):
        raise RuntimeError("health write failed")

    monkeypatch.setattr(health, "record_health", boom)
    with caplog.at_level("ERROR", logger="thread_archive._watcher.daemon"):
        w._record_pass()  # advisory — must not raise
    assert any("could not record pass heartbeat" in r.getMessage() for r in caplog.records)


def test_maintain_folds_meta_docs_when_present(archive_home, monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(fts, "index_thread_meta", lambda: 7)
    counts = Watcher([], embed=False).maintain()
    assert counts.get("thread_meta_docs") == 7


def test_maintain_skips_meta_docs_when_empty(archive_home, monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(fts, "index_thread_meta", lambda: 0)
    counts = Watcher([], embed=False).maintain()
    assert "thread_meta_docs" not in counts  # falsy meta → not folded


def test_maintain_survives_meta_index_error(archive_home, monkeypatch, caplog) -> None:
    init_db()

    def boom():
        raise RuntimeError("fts exploded")

    monkeypatch.setattr(fts, "index_thread_meta", boom)
    with caplog.at_level("WARNING", logger="thread_archive._watcher.daemon"):
        counts = Watcher([], embed=False).maintain()  # must not raise
    assert isinstance(counts, dict)
    assert any("thread-meta index error" in r.getMessage() for r in caplog.records)


def test_embed_pending_returns_zero_when_caught_up(archive_home, monkeypatch) -> None:
    monkeypatch.setattr(vectors, "index_events_local",
                        lambda max_events=None, newest_first=False: 0)
    assert Watcher([], embed_batch=64).embed_pending() == 0


def test_run_warns_when_owner_lock_unavailable(archive_home, monkeypatch, caplog) -> None:
    """When another process already holds the ingest-owner lock, ``run`` logs and
    proceeds rather than dying (which would feed launchd's restart throttle)."""
    monkeypatch.setattr(lazy, "acquire_ingest_owner", lambda: None)
    w = Watcher([], embed=False)
    ran = {"n": 0}
    w._run_loop = lambda: ran.__setitem__("n", ran["n"] + 1)
    with caplog.at_level("WARNING", logger="thread_archive._watcher.daemon"):
        w.run()  # owner_fd is None → the finally must not try to close a fd
    assert ran["n"] == 1
    assert any("another process holds the ingest-owner lock" in r.getMessage()
               for r in caplog.records)


def test_run_loop_skips_pass_while_reindex_holds_lock(archive_home, monkeypatch, caplog) -> None:
    w = Watcher([_StubSource("s")], interval=0.01, embed=False)

    @contextmanager
    def unacquired():
        w._stop = True  # one tick, then fall out of the while loop
        yield False

    monkeypatch.setattr(truth, "try_shared_ingest_lock", unacquired)
    with caplog.at_level("INFO", logger="thread_archive._watcher.daemon"):
        w._run_loop()
    assert w.watchers[0].polls == 0  # lock not acquired → poll skipped
    assert any("reindex in progress" in r.getMessage() for r in caplog.records)


def test_run_loop_full_tick_runs_maintenance_and_embed(archive_home, monkeypatch) -> None:
    """One acquired tick: an importing poll marks the loop dirty, the (due)
    maintenance pass runs, and the embed cohost drains — a filled batch flags a
    backlog for the next cycle. The tick then stops the loop."""
    stub = _StubSource("s", events=1)
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed_interval=0.0,
                embed=True, embed_batch=1)

    @contextmanager
    def acquired():
        yield True

    monkeypatch.setattr(truth, "try_shared_ingest_lock", acquired)
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


def test_run_loop_maintenance_due_but_nothing_changed_skips_it(archive_home, monkeypatch) -> None:
    """Maintenance interval elapsed, but the poll imported nothing (not dirty) and
    no watermark moved (stamp None) → the maintenance pass is skipped."""
    stub = _StubSource("s", events=0)  # nothing imported → stays clean
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed=False)

    @contextmanager
    def acquired_one_tick():
        yield True
        w._stop = True  # post-tick (after poll/maintenance/embed) → loop ends

    monkeypatch.setattr(truth, "try_shared_ingest_lock", acquired_one_tick)
    calls = {"maintain": 0}
    w.maintain = lambda: calls.__setitem__("maintain", calls["maintain"] + 1) or {}
    w._import_state_stamp = lambda: None  # no watermark → maintenance condition False

    w._run_loop()

    assert stub.polls == 1
    assert calls["maintain"] == 0  # due, but nothing changed → skipped


def test_run_loop_survives_maintenance_and_embed_errors(archive_home, monkeypatch, caplog) -> None:
    """Maintenance and embed both raise on the tick; each is caught so the loop
    survives (a raise here would exit the process into launchd's restart churn)."""
    stub = _StubSource("s", events=1)
    w = Watcher([stub], interval=0.01, maintenance_interval=0.0, embed_interval=0.0,
                embed=True, embed_batch=8)

    @contextmanager
    def acquired():
        yield True

    monkeypatch.setattr(truth, "try_shared_ingest_lock", acquired)
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
