"""The watcher detects changed source files, imports new
events, and doesn't duplicate on repeated polls.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select

from thread_archive.store import Event, get_session, init_db
from thread_archive.watcher import ClaudeCodeWatcher, Watcher, cursor_watcher

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


def test_cloth_watcher_detects_imports_and_self_gates(archive_home, tmp_path) -> None:
    """cloth is a plain provider watcher like the rest: it imports its store's
    sessions under ``source="cloth"`` with the bare file stem as ``source_id``,
    dedups on re-poll, sits in
    the default set, and self-gates (inert, process-free) when the cloth home is absent."""
    from sqlalchemy import select

    from thread_archive.store import Thread
    from thread_archive.watcher import cloth_watcher
    from thread_archive.watcher.sources import default_watchers

    assert "cloth" in [w.source_name for w in default_watchers()]
    assert not cloth_watcher(threads_dir=tmp_path / "no-cloth-here").is_available()

    init_db()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / "63.jsonl").write_text(
        "\n".join(json.dumps(ln) for ln in [USER, ASSISTANT]) + "\n", encoding="utf-8"
    )

    w = cloth_watcher(threads_dir=threads_dir)
    assert w.is_available()

    r1 = w.poll()
    assert r1.items_imported == 1 and r1.events_created > 0
    n1 = _event_count()
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "cloth")).scalar_one()
        assert t.source_id == "63"  # bare session-file stem, no prefix

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
    from thread_archive.retrieval import vectors as V

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
    from thread_archive.truth.jsonl_log import _reindex_lock_path
    from thread_archive.watcher.base import WatchResult

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
