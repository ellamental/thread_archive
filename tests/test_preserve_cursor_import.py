"""Drop-site regressions for the Cursor DB importer: no provider record may be
silently skipped/dropped/truncated on import (thread's "capture EVERYTHING").

Covers the drop-sites guarded in ``importers/cursor.py``:
1. An unknown bubble type must not collapse to a bare ``{"role": role}`` —
   that drops the bubble's content/thinking/tool_call/raw. It must be preserved
   as a shared ``message`` event carrying all of it.
2. A corrupt composer/bubble blob must not vanish (silent ``continue`` on a JSON
   error, or logged-and-dropped ``skipping`` on a blow-up). It must surface a
   stub thread carrying id + raw + error, or (for a corrupt referenced bubble) be
   preserved as an unknown ``message`` event.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select

from thread_archive._importers import import_cursor_db
from thread_archive._importers.cursor import _cursor_to_normalized
from thread_archive._store import Event, Thread, get_session, init_db


def _events():
    with get_session() as s:
        return [(e.event_type, e.payload) for e in s.execute(select(Event)).scalars()]


def _threads():
    with get_session() as s:
        return {t.source_id: t for t in s.execute(select(Thread)).scalars()}


# ── 1. Unknown bubble type is preserved (not dropped to {"role": role}) ───────


def test_unknown_bubble_type_preserved_as_message_event(archive_home) -> None:
    """An unmodeled bubble type (not 1/user, 2/assistant) keeps its content,
    thinking, tool_call, and full raw as a `message` event — the old bare
    {"role": role} silently discarded the whole turn."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp_unknown"
    composer = {
        "name": "Unknown Type Chat",
        "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2", "type": 99},  # unmodeled type
        ],
    }
    weird_bubble = {
        "type": 99,
        "text": "some unknown-type content",
        "createdAt": 1700000001000,
        "thinking": {"text": "unknown-type reasoning"},
        "toolFormerData": {
            "name": "mystery_tool", "toolCallId": "toolu_x", "status": "completed",
            "params": {"q": "?"}, "result": {"ok": True},
        },
        "someProviderOnlyField": "must-survive",
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", json.dumps(weird_bubble)),
    ])
    conn.commit()
    conn.close()

    scan = import_cursor_db(db)
    assert scan.composers_imported == 1

    events = _events()
    msgs = [p for (t, p) in events if t == "message"]
    assert msgs, "unknown bubble type was dropped instead of preserved as a message event"
    payload = msgs[0]
    # role, content, and content_blocks all survive.
    assert payload["role"] == "unknown"
    assert payload["content"] == "some unknown-type content"
    blob = json.dumps(payload)
    assert "unknown-type reasoning" in blob          # thinking preserved
    assert "mystery_tool" in blob and "toolu_x" in blob  # tool_call preserved
    assert "must-survive" in blob                     # full raw bubble preserved


def test_cursor_to_normalized_unknown_role_carries_content_not_bare_dict() -> None:
    """Direct unit check: the mapper must not return a bare {"role": role} for an
    unmodeled role — it carries content_text, content_blocks, and raw."""
    msg = {
        "id": "bx", "role": "unknown", "content": "keep me",
        "created_at": 1700000000000, "index": 0,
        "thinking": "keep this thought",
        "tool_call": {"name": "t", "call_id": "c1", "input": {"a": 1}, "result": "done"},
        "bubble_type": 7, "raw": {"raw_only": "x"},
    }
    out = _cursor_to_normalized(msg)
    assert out["role"] == "unknown"
    assert out["content_text"] == "keep me"
    assert out["content_blocks"], "content_blocks should not be empty"
    assert out["provider_data"].get("raw") == {"raw_only": "x"}
    blob = json.dumps(out)
    assert "keep this thought" in blob and "raw_only" in blob


def test_modeled_user_assistant_still_work(archive_home) -> None:
    """Guard: the preserve changes don't regress the modeled user/assistant path."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp_ok"
    composer = {
        "name": "Normal Chat", "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}],
    }
    tool_bubble = {
        "type": 2, "text": "reading", "createdAt": 1700000001000,
        "toolFormerData": {
            "name": "read_file", "toolCallId": "toolu_ok", "status": "completed",
            "params": {"targetFile": "/x"}, "result": {"contents": "hello world"},
        },
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "read /x", "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", json.dumps(tool_bubble)),
    ])
    conn.commit()
    conn.close()

    import_cursor_db(db)
    types = {t for (t, _p) in _events()}
    assert "user_message_sent" in types
    assert "tool_use_complete" in types and "tool_execution_completed" in types


# ── 2. Corrupt blobs surface a stub instead of vanishing ──────────────────────


def test_corrupt_composer_preserved_as_stub_thread(archive_home) -> None:
    """A composer whose JSON is corrupt must not vanish on a silent `continue`:
    it must surface a stub thread carrying the raw + error."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("composerData:broken", "{not valid json at all"),
    )
    conn.commit()
    conn.close()

    import_cursor_db(db)  # must not raise
    threads = _threads()
    assert "broken:import-error" in threads, "corrupt composer vanished with no stub"
    blob = json.dumps(_events())
    assert "not valid json at all" in blob, "raw payload of the corrupt composer was lost"


def test_composer_import_blowup_preserved_as_stub(archive_home, monkeypatch) -> None:
    """If a composer blows up mid-import, the conversation must not be skipped
    with only a log line: it must surface a stub thread carrying id + raw + error."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp_boom"
    composer = {
        "name": "Boom Chat", "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}],
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
    ])
    conn.commit()
    conn.close()

    import thread_archive._importers.cursor as cursor_mod

    def _boom(*a, **k):
        raise RuntimeError("simulated importer failure")

    monkeypatch.setattr(cursor_mod, "import_cursor_from_payload", _boom)

    import_cursor_db(db)  # must not raise; must preserve a stub
    threads = _threads()
    assert f"{cid}:import-error" in threads, "blown-up composer was skipped with no stub"
    blob = json.dumps(_events())
    assert "simulated importer failure" in blob, "the error was not preserved on the stub"


def test_corrupt_referenced_bubble_preserved_not_dropped(archive_home) -> None:
    """A referenced bubble whose JSON is corrupt must not vanish on a silent
    `continue`: its raw must survive via the unknown-bubble `message` path."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    cid = "comp_badbubble"
    composer = {
        "name": "Bad Bubble Chat", "lastUpdatedAt": 1700000000000,
        "fullConversationHeadersOnly": [
            {"bubbleId": "b1", "type": 1},
            {"bubbleId": "b2"},  # references the corrupt bubble
        ],
    }
    conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
        (f"composerData:{cid}", json.dumps(composer)),
        (f"bubbleId:{cid}:b1", json.dumps({"type": 1, "text": "hi", "createdAt": 1700000000000})),
        (f"bubbleId:{cid}:b2", "{corrupt-bubble-json"),
    ])
    conn.commit()
    conn.close()

    import_cursor_db(db)  # must not raise
    blob = json.dumps(_events())
    assert "corrupt-bubble-json" in blob, "raw of the corrupt referenced bubble was dropped"


def test_stub_import_is_idempotent(archive_home) -> None:
    """Re-scanning a still-corrupt composer must not restack stub events."""
    init_db()
    db = archive_home / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:dupe", "{still broken")
    )
    conn.commit()
    conn.close()

    import_cursor_db(db)
    n = len(_events())
    import_cursor_db(db)  # second scan of the same corrupt blob
    assert len(_events()) == n, "corrupt-composer stub restacked on re-scan"
