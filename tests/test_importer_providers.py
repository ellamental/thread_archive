"""The remaining provider importers: import a fixture transcript for
each, and prove repeated import is idempotent.

Line-stream providers (Codex, Grok, Antigravity) take a single JSONL file; the
SQLite scanners (Cursor, OpenCode) take a DB of many sessions.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select

from thread_archive.importers import (
    import_antigravity_session_incremental,
    import_cloth_session_incremental,
    import_codex_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
)
from thread_archive.store import Event, Thread, get_session, init_db


def _event_count() -> int:
    with get_session() as s:
        return len(s.execute(select(Event)).scalars().all())


def _thread_for(source: str) -> Thread:
    with get_session() as s:
        return s.execute(select(Thread).where(Thread.source == source)).scalar_one()


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


# ── Codex ───────────────────────────────────────────────────────────────────

CODEX = [
    {"type": "session_meta", "payload": {"id": "sess", "cwd": "/proj", "model": "gpt-5"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:00Z",
     "payload": {"type": "user_message", "message": "what is 2+2", "turn_id": "t1"}},
    {"type": "event_msg", "timestamp": "2026-01-01T10:00:05Z",
     "payload": {"type": "agent_message", "message": "4"}},
]


def test_codex_import_and_idempotent(archive_home) -> None:
    init_db()
    f = archive_home / "codex.jsonl"
    _write_jsonl(f, CODEX)

    r = import_codex_session_incremental(f, "codex-sess")
    assert r.is_new_thread and r.events_created > 0
    n = _event_count()
    thread = _thread_for("codex")
    assert thread.source_metadata == {"cwd": "/proj"}
    assert thread.title == "what is 2+2"

    r2 = import_codex_session_incremental(f, "codex-sess")
    assert r2.events_created == 0
    assert _event_count() == n


# ── cloth ───────────────────────────────────────────────────────────────────

CLOTH = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T10:00:00Z", "sessionId": "cloth-7",
     "message": {"role": "user", "content": "hello cloth"}},
    {"type": "cloth_meta", "uuid": "m1", "parentUuid": "u1",
     "timestamp": "2026-01-01T10:00:00.5Z", "sessionId": "cloth-7",
     "meta": {"client": "cloth", "model": "deepseek/deepseek-v4-pro"}},
    {"type": "assistant", "uuid": "a1", "timestamp": "2026-01-01T10:00:05Z", "sessionId": "cloth-7",
     "message": {"role": "assistant", "model": "deepseek/deepseek-v4-pro",
                 "content": [{"type": "text", "text": "hi from cloth"}]}},
]


def test_cloth_import_and_idempotent(archive_home) -> None:
    """cloth is a first-class provider: its own importer, labeled source='cloth'
    (not claude-code), with the ``cloth_meta`` line absorbed by the shared parser."""
    init_db()
    f = archive_home / "cloth.jsonl"
    _write_jsonl(f, CLOTH)

    r = import_cloth_session_incremental(f, "cloth-cli-7")
    assert r.is_new_thread and r.events_created > 0
    n = _event_count()
    thread = _thread_for("cloth")
    assert thread.source == "cloth"
    assert thread.source_id == "cloth-cli-7"
    # The cloth_meta line yields no junk thread under another source.
    with get_session() as s:
        assert s.execute(select(Thread).where(Thread.source == "claude-code")).first() is None

    r2 = import_cloth_session_incremental(f, "cloth-cli-7")
    assert r2.events_created == 0
    assert _event_count() == n


# ── Antigravity ─────────────────────────────────────────────────────────────

ANTIGRAVITY = [
    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "done",
     "created_at": "2026-01-01T10:00:00Z", "content": "<USER_REQUEST>fix the bug</USER_REQUEST>"},
    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "done",
     "created_at": "2026-01-01T10:00:05Z", "content": "On it — fixing the bug."},
]


def test_antigravity_import_and_idempotent(archive_home) -> None:
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, ANTIGRAVITY)

    r = import_antigravity_session_incremental(f, "ag-conv")
    assert r.is_new_thread and r.events_created > 0
    n = _event_count()
    assert _thread_for("antigravity").title == "fix the bug"

    r2 = import_antigravity_session_incremental(f, "ag-conv")
    assert r2.events_created == 0
    assert _event_count() == n


# ── Grok ────────────────────────────────────────────────────────────────────

GROK = [
    {"type": "user", "content": [{"type": "text", "text": "<user_query>hello grok</user_query>"}]},
    {"type": "assistant", "content": "hi from grok", "tool_calls": []},
]


def test_grok_import_and_idempotent(archive_home) -> None:
    init_db()
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    f = session_dir / "chat_history.jsonl"
    _write_jsonl(f, GROK)

    r = import_grok_session_incremental(f, "grok-sess")
    assert r.is_new_thread and r.events_created > 0
    n = _event_count()
    thread = _thread_for("grok")
    assert thread.title == "hello grok"

    r2 = import_grok_session_incremental(f, "grok-sess")
    assert r2.events_created == 0
    assert _event_count() == n


# ── Cursor (SQLite scanner) ─────────────────────────────────────────────────

def _make_cursor_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    composer_id = "comp1"
    composer = {
        "name": "My Cursor Chat",
        "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}],
    }
    rows = [
        (f"composerData:{composer_id}", json.dumps(composer)),
        (f"bubbleId:{composer_id}:b1", json.dumps({"type": 1, "text": "hello cursor", "createdAt": 1700000000000})),
        (f"bubbleId:{composer_id}:b2", json.dumps({"type": 2, "text": "hi from cursor"})),
    ]
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_cursor_db_scan_and_idempotent(archive_home) -> None:
    init_db()
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)

    scan = import_cursor_db(db)
    assert scan.composers_processed == 1
    assert scan.composers_imported == 1
    assert scan.events_created > 0
    n = _event_count()
    thread = _thread_for("cursor")
    assert thread.title == "My Cursor Chat"

    scan2 = import_cursor_db(db)
    assert scan2.composers_imported == 0  # lastUpdatedAt < last_import → skipped
    assert _event_count() == n


def test_cursor_full_reimport_does_not_restack_null_dedup_key(archive_home) -> None:
    """Regression: the March-2026 Postgres backfill seeded events with NULL dedup_key,
    invisible to the key-based dedup. A later full re-import (import_state reset / a
    manual migration re-run) then stacked duplicate events. The cross_pass_dedup guard
    must make that re-import a no-op even though the existing rows have no dedup_key."""
    init_db()
    db = archive_home / "state.vscdb"
    _make_cursor_db(db)
    import_cursor_db(db)
    n = _event_count()
    assert n > 0

    # Recreate the backfill condition: strip dedup_keys (as the PG-seeded rows had)
    # and clear the import cursor so the next scan is a full re-import from index 0.
    from thread_archive.store import ImportState
    with get_session() as s:
        s.query(Event).update({Event.dedup_key: None})
        s.query(ImportState).filter(ImportState.source == "cursor").delete()
        s.commit()

    scan2 = import_cursor_db(db)  # full re-import against null-keyed existing rows
    assert scan2.events_created == 0, "re-import re-stacked events the dedup_key couldn't see"
    assert _event_count() == n


def test_cursor_emits_tool_events(archive_home) -> None:
    """Regression: the cursor importer dropped every tool call on parse — an
    assistant bubble's toolFormerData was ignored, so a re-parse lost all
    tool_use_complete / tool_execution_completed events. It must now emit both."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp_tool"
    composer = {
        "name": "Tool Chat",
        "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2", "type": 2},
        ],
    }
    tool_bubble = {
        "type": 2, "text": "reading the file", "createdAt": 1700000001000,
        "toolFormerData": {
            "name": "read_file", "tool": 40, "toolCallId": "toolu_abc", "status": "completed",
            "rawArgs": json.dumps({"target_file": "/x"}),
            "params": {"targetFile": "/x"},
            "result": {"contents": "hello world"},
        },
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "read /x", "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", json.dumps(tool_bubble)),
    ])
    conn.commit(); conn.close()

    import_cursor_db(db)
    with get_session() as s:
        types = {t for (t,) in s.execute(select(Event.event_type))}
    assert "tool_use_complete" in types, "tool call was dropped on parse"
    assert "tool_execution_completed" in types, "tool result was dropped on parse"
    with get_session() as s:
        use = s.execute(select(Event.payload).where(Event.event_type == "tool_use_complete")).scalar()
        res = s.execute(select(Event.payload).where(Event.event_type == "tool_execution_completed")).scalar()
    assert use["tool_call_id"] == "toolu_abc" and use["tool_name"] == "read_file"
    assert res["tool_call_id"] == "toolu_abc" and "hello world" in json.dumps(res["output"])


# ── OpenCode (SQLite scanner) ───────────────────────────────────────────────

def _make_opencode_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT, project_id TEXT, parent_id TEXT, title TEXT, "
        "directory TEXT, time_created INTEGER, time_updated INTEGER, agent TEXT)"
    )
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    sid = "s1"
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
        (sid, "proj", None, "My OpenCode Session", "/proj", 1700000000000, 1700000005000, "build"),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m1", sid, 1700000000000, json.dumps({"role": "user", "time": {"created": 1700000000000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p1", "m1", sid, 1700000000000,
         json.dumps({"type": "text", "text": "hello opencode", "time": {"start": 1700000000000}})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m2", sid, 1700000001000, json.dumps(
            {"role": "assistant", "modelID": "gpt", "providerID": "oai",
             "time": {"created": 1700000001000, "completed": 1700000002000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p2", "m2", sid, 1700000001000,
         json.dumps({"type": "text", "text": "hi from opencode", "time": {"start": 1700000001000}})),
    )
    conn.commit()
    conn.close()


def test_opencode_db_scan_and_idempotent(archive_home) -> None:
    init_db()
    db = archive_home / "opencode.db"
    _make_opencode_db(db)

    scan = import_opencode_db(db)
    assert scan.sessions_processed == 1
    assert scan.sessions_imported == 1
    assert scan.events_created > 0
    n = _event_count()
    thread = _thread_for("opencode")
    assert thread.title == "My OpenCode Session"

    scan2 = import_opencode_db(db)
    assert scan2.sessions_imported == 0  # time_updated < last_import → skipped
    assert _event_count() == n
