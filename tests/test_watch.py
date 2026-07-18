"""The watcher detects changed source files, imports new
events, and doesn't duplicate on repeated polls.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select

from thread_archive._store import Event, get_session, init_db
from thread_archive._watcher import ClaudeCodeWatcher, Watcher, cursor_watcher

USER = {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z",
        "cwd": "/proj", "message": {"role": "user", "content": "hello watcher"}}
ASSISTANT = {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"role": "assistant", "model": "claude-opus-4",
                         "content": [{"type": "text", "text": "hi back"}]}}


def _event_count() -> int:
    with get_session() as s:
        return len(s.execute(select(Event)).scalars().all())


def _write_cc(projects_root, project, name, lines):
    pdir = projects_root / project
    pdir.mkdir(parents=True, exist_ok=True)
    f = pdir / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    return f


def test_claude_code_watcher_detects_imports_and_dedups(archive_home, tmp_path) -> None:
    init_db()
    projects = tmp_path / "projects"
    f = _write_cc(projects, "myproj", "sess", [USER, ASSISTANT])

    w = ClaudeCodeWatcher(projects_dirs=[projects])
    assert w.is_available()

    r1 = w.poll()
    assert r1.items_imported == 1 and r1.events_created > 0
    n1 = _event_count()

    # Unchanged file → fingerprint skip, no new events.
    r2 = w.poll()
    assert r2.events_created == 0
    assert _event_count() == n1

    # Append a new turn → the watcher detects the change and imports just it.
    user2 = {**USER, "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
             "message": {"role": "user", "content": "a second question"}}
    assistant2 = {**ASSISTANT, "uuid": "a2", "timestamp": "2026-01-01T10:01:05Z",
                  "message": {"role": "assistant", "model": "claude-opus-4",
                              "content": [{"type": "text", "text": "a second answer"}]}}
    f.write_text("\n".join(json.dumps(ln) for ln in [USER, ASSISTANT, user2, assistant2]) + "\n",
                 encoding="utf-8")

    r3 = w.poll()
    assert r3.events_created > 0
    assert _event_count() > n1


def test_claude_code_watcher_unavailable_when_dir_absent(archive_home, tmp_path) -> None:
    w = ClaudeCodeWatcher(projects_dirs=[tmp_path / "does-not-exist"])
    assert not w.is_available()


def test_rglob_watcher_imports_cc_shaped_source_and_self_gates(archive_home, tmp_path) -> None:
    """The two properties every provider plugin's file watcher depends on, proven
    through the public API a plugin is written against.

    A harness writing Claude-Code-shaped JSONL reuses that parser via
    ``claude_code_line_stream`` while keeping its own source identity, so its
    threads carry its provenance instead of masquerading as Claude Code — and the
    ``source_id`` is whatever ``source_id_of`` derives, here the bare file stem.
    The watcher also self-gates: a store that isn't on this machine reports
    unavailable, so a provider nobody has installed costs nothing per poll.
    """
    from sqlalchemy import select

    from thread_archive._store import Thread
    from thread_archive.provider import RglobWatcher, claude_code_line_stream

    def _watcher(root):
        return RglobWatcher(
            root, claude_code_line_stream("demo-harness"), lambda p: p.stem,
            name="demo-harness",
        )

    assert not _watcher(tmp_path / "no-store-here").is_available()

    init_db()
    store = tmp_path / "sessions"
    store.mkdir()
    (store / "63.jsonl").write_text(
        "\n".join(json.dumps(ln) for ln in [USER, ASSISTANT]) + "\n", encoding="utf-8"
    )

    w = _watcher(store)
    assert w.is_available()

    r1 = w.poll()
    assert r1.items_imported == 1 and r1.events_created > 0
    n1 = _event_count()
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "demo-harness")).scalar_one()
        assert t.source_id == "63"  # bare session-file stem, no prefix
        # Reusing Claude Code's parser must not store the thread as Claude Code.
        assert s.execute(select(Thread).where(Thread.source == "claude-code")).first() is None

    # Unchanged file → fingerprint skip, no duplication.
    r2 = w.poll()
    assert r2.events_created == 0
    assert _event_count() == n1


def _make_cursor_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    comp = {"name": "Watched Cursor Chat", "lastUpdatedAt": 1700000000000,
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]}
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        ("composerData:c1", json.dumps(comp)),
        ("bubbleId:c1:b1", json.dumps({"type": 1, "text": "hello cursor", "createdAt": 1700000000000})),
        ("bubbleId:c1:b2", json.dumps({"type": 2, "text": "hi cursor"})),
    ])
    conn.commit()
    conn.close()


def test_cursor_db_watcher_mtime_gate(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "state.vscdb"
    _make_cursor_db(db)

    w = cursor_watcher(db_path=db)
    assert w.is_available()

    r1 = w.poll()
    assert r1.items_imported == 1 and r1.events_created > 0
    n1 = _event_count()

    # mtime unchanged → skipped wholesale.
    r2 = w.poll()
    assert r2.events_created == 0
    assert _event_count() == n1


def test_watcher_poll_once_is_inline_durable(archive_home, tmp_path) -> None:
    """A poll that imports makes events durable in the truth log inline — no
    checkpoint in the hot loop. The per-thread file exists right after poll_once."""
    init_db()
    projects = tmp_path / "projects"
    _write_cc(projects, "myproj", "sess", [USER, ASSISTANT])

    watcher = Watcher([ClaudeCodeWatcher(projects_dirs=[projects])], interval=1.0)
    result = watcher.poll_once()
    assert result.events_created > 0

    # The import wrote a per-thread truth file before its COMMIT (inline durability),
    # with no checkpoint — so the manifest is not written by poll_once.
    assert list((archive_home / "truth" / "threads").rglob("*.jsonl"))
    assert not (archive_home / "truth" / "manifest.json").exists()


def test_watcher_run_loop_survives_a_failing_pass(archive_home, caplog) -> None:
    """An exception escaping the pass body (lock acquisition, a poll bug) must
    not exit the loop — process death means launchd restarts every few seconds
    with all source fingerprints reset, a hot re-scan loop on persistent faults.
    The loop logs, backs off, and keeps going; a healing pass resets the backoff."""
    import threading
    import time

    init_db()
    w = Watcher([], interval=0.01)
    calls = {"n": 0}

    def flaky_poll_once():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("simulated pass failure")
        return type(w).poll_once(w)

    w.poll_once = flaky_poll_once
    t = threading.Thread(target=w._run_loop)
    with caplog.at_level("ERROR", logger="thread_archive._watcher.daemon"):
        t.start()
        deadline = time.monotonic() + 5.0
        while calls["n"] < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        w.stop()
        t.join(timeout=5.0)
    assert not t.is_alive()
    assert calls["n"] >= 4, "the loop must survive failing passes and keep polling"
    assert sum("ingest pass failed" in r.getMessage() for r in caplog.records) == 2


def test_watcher_maintenance_writes_manifest_not_overlays(archive_home, tmp_path) -> None:
    """maintain() runs the cheap upkeep (manifest/rebalance) but does NOT rewrite the
    cross-thread overlay snapshots — conversation ingest never changes them."""
    init_db()
    projects = tmp_path / "projects"
    _write_cc(projects, "myproj", "sess", [USER, ASSISTANT])

    watcher = Watcher([ClaudeCodeWatcher(projects_dirs=[projects])], interval=1.0)
    watcher.poll_once()
    watcher.maintain()

    truth = archive_home / "truth"
    assert (truth / "manifest.json").exists()
    # The overlays are owned by migration + the kg_events log, not the ingest path.
    assert not (truth / "thread_links.jsonl").exists()
    assert not (truth / "topic_messages.jsonl").exists()


def test_watcher_embed_pending_delegates_bounded(archive_home, monkeypatch) -> None:
    """embed_pending() drains the freshest gap, bounded by embed_batch — it asks the
    incremental embedder for the newest missing vectors, capped."""
    from thread_archive._retrieval import vectors as V

    seen = {}
    monkeypatch.setattr(
        V, "index_events_local",
        lambda max_events=None, newest_first=False: (
            seen.update(max_events=max_events, newest_first=newest_first) or 9),
    )
    w = Watcher([], embed_batch=256)
    assert w.embed_pending() == 9
    assert seen == {"max_events": 256, "newest_first": True}


def test_watcher_embed_cohost_flags_default_on() -> None:
    """The vector cohost is on by default (so the lag can't reopen), with a startup
    backlog hint set; --no-embed turns it off."""
    on = Watcher([])
    assert on.embed_enabled is True and on.embed_batch == 512 and on._embed_more is True
    off = Watcher([], embed=False)
    assert off.embed_enabled is False


def test_watcher_embed_backlog_drains_every_cycle() -> None:
    """The idle probe waits out embed_interval, but a draining backlog (last pass
    filled its batch) runs the cohost every poll cycle — a bulk write becomes
    searchable in minutes, not hours."""
    w = Watcher([], embed_interval=300.0)
    assert w._backlog is False
    # idle: not due until the interval elapses
    assert w._embed_due(now=10.0, last_embed=5.0) is False
    assert w._embed_due(now=310.0, last_embed=5.0) is True
    # backlog: due immediately, interval ignored
    w._backlog = True
    assert w._embed_due(now=10.0, last_embed=5.0) is True
    # nothing pending / cohost off: never due
    w._embed_more = False
    assert w._embed_due(now=10.0, last_embed=5.0) is False


def test_watcher_available_filters_missing_sources(archive_home, tmp_path) -> None:
    projects = tmp_path / "projects"
    _write_cc(projects, "p", "s", [USER, ASSISTANT])
    watcher = Watcher(
        [ClaudeCodeWatcher(projects_dirs=[projects]),
         cursor_watcher(db_path=tmp_path / "missing.vscdb")],
        interval=1.0,
    )
    available = [w.source_name for w in watcher.available()]
    assert "claude-code" in available
    assert "cursor" not in available


def test_watch_once_holds_shared_ingest_lock(archive_home, monkeypatch, capsys):
    """``archive watch --once`` holds the shared ingest lock like every other
    truth writer, so a concurrent exclusive reindex can't interleave."""
    import fcntl
    import os

    from thread_archive import cli
    from thread_archive._truth.jsonl_log import _reindex_lock_path
    from thread_archive._watcher.base import WatchResult

    seen = {}

    def fake_poll(self):
        # A shared holder must block an exclusive probe (distinct fd = distinct
        # flock owner, even in-process).
        fd = os.open(_reindex_lock_path(), os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked"] = False
            except OSError:
                seen["locked"] = True
        finally:
            os.close(fd)
        return WatchResult()

    monkeypatch.setattr(Watcher, "poll_once", fake_poll)
    assert cli.main(["watch", "--once"]) == 0
    assert seen["locked"] is True


def test_failed_items_inside_a_db_scan_surface_too(archive_home, tmp_path, monkeypatch) -> None:
    """A DB scan catches per-item failures so one bad conversation can't abort it. But
    a caught failure that only reaches the log leaves the scan looking like a clean
    "nothing new" — the source stays green while a conversation is missing. The failure
    has to ride out to the poll's errors."""
    from thread_archive._importers import cursor as cursor_importer

    init_db()
    db = tmp_path / "state.vscdb"
    _make_cursor_db(db)

    def boom(**kwargs):
        raise RuntimeError("composer shape changed")

    monkeypatch.setattr(cursor_importer, "import_cursor_from_payload", boom)

    res = cursor_watcher(db_path=db).poll()

    assert res.events_created == 0
    assert res.errors and "cursor" in res.errors[0]
    assert "composer shape changed" in res.errors[0]


def test_poll_errors_surface_in_health(archive_home) -> None:
    """A failing source must be visible to `archive status` (health.json), not
    only to whoever reads the daemon's stderr log — a provider format change
    could otherwise stall one source's ingest for weeks while status stays green."""
    from thread_archive import _api as ta
    from thread_archive._watcher.base import SourceWatcher

    class Broken(SourceWatcher):
        source_name = "broken-source"

        def poll(self):
            raise RuntimeError("provider format changed")

        def is_available(self):
            return True

    w = Watcher(watchers=[Broken()], embed=False)
    res = w.poll_once()
    assert res.errors and "broken-source" in res.errors[0]

    rec = ta.status()["last_watch_errors"]
    assert rec is not None
    assert rec["count_since_start"] == 1
    assert "broken-source" in rec["errors"][0]
    assert rec["at"]


def test_locked_out_commit_self_heals_on_next_poll(archive_home, tmp_path, monkeypatch) -> None:
    """A commit that dies with SQLITE_BUSY mid-poll (a maintenance transaction
    holding the write lock past busy_timeout) must cost exactly one poll of
    latency: the events+watermark transaction rolls back together, the error
    rides the poll result, and the next poll re-imports to exactly-once —
    truth (which is written ahead of the commit) and index agree afterward."""
    import sqlalchemy.orm

    from thread_archive import _api as ta

    init_db()
    projects = tmp_path / "projects"
    f = _write_cc(projects, "myproj", "sess", [USER, ASSISTANT])
    w = ClaudeCodeWatcher(projects_dirs=[projects])
    assert w.poll().items_imported == 1
    n1 = _event_count()

    user2 = {**USER, "uuid": "u2", "timestamp": "2026-01-01T10:01:00Z",
             "message": {"role": "user", "content": "the locked-out turn"}}
    f.write_text("\n".join(json.dumps(ln) for ln in [USER, ASSISTANT, user2]) + "\n",
                 encoding="utf-8")

    real_commit = sqlalchemy.orm.Session.commit
    state = {"failed": 0}

    def flaky_commit(self):
        state["failed"] += 1
        self.rollback()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", flaky_commit)
    r_fail = w.poll()
    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", real_commit)

    assert state["failed"] >= 1
    assert r_fail.errors and "database is locked" in r_fail.errors[0]
    assert _event_count() == n1  # nothing half-landed in the index

    r_heal = w.poll()
    assert r_heal.events_created > 0
    assert _event_count() == n1 + 1  # exactly the one new event, once

    ta.checkpoint()
    v = ta.verify()
    assert v["ok"], v  # truth and index converged (truth-ahead is collapsed)
