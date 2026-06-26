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
    sessions under ``source="cloth"`` / ``cloth-cli-<n>``, dedups on re-poll, sits in
    the default set, and self-gates (inert, process-free) when ``~/.cloth`` is absent."""
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
        assert t.source_id == "cloth-cli-63"

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
