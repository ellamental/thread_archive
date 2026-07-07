"""Capture-everything regression tests for the OpenCode importer.

The archive must never silently drop a provider record on import. These cover the
five drop-sites that were fixed in ``importers/opencode.py``:

1. non-user/assistant messages (were ``continue``-skipped),
2. non-text user parts — files/attachments (were filtered out),
3. unknown assistant part types (were ``seg=None``-dropped),
4. malformed message/part JSON (were swallowed by ``except: continue``),
5. an abandoned/unsettled assistant turn (the settled-prefix ``break`` stranded it
   and everything after it forever — now staleness-gated).

Follows the DB-scanner pattern from ``tests/test_importer_providers.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import select

from thread_archive.importers import import_opencode_db
from thread_archive.store import Event, get_session, init_db

# A session timestamp far enough in the past that it always reads as abandoned
# (> 24h since last update); and one at "now" that always reads as live.
_STALE_TS = 1700000000000  # 2023 — years before any test run
_FRESH_TS = int(datetime.now(timezone.utc).timestamp() * 1000)


def _make_db(path, *, time_updated: int, messages) -> None:
    """Build a one-session opencode DB.

    ``messages`` is a list of ``(msg_id, msg_data, parts)`` where ``msg_data`` and
    each part's data may be a dict (json-encoded) or a raw ``str`` inserted verbatim
    (to simulate on-disk corruption). ``parts`` is a list of ``(part_id, part_data)``.
    """
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
        (sid, "proj", None, "Sess", "/proj", 1700000000000, time_updated, "build"),
    )
    for i, (msg_id, msg_data, parts) in enumerate(messages):
        raw = msg_data if isinstance(msg_data, str) else json.dumps(msg_data)
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", (msg_id, sid, 1700000000000 + i, raw))
        for j, (part_id, part_data) in enumerate(parts):
            praw = part_data if isinstance(part_data, str) else json.dumps(part_data)
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (part_id, msg_id, sid, 1700000000000 + i * 100 + j, praw),
            )
    conn.commit()
    conn.close()


def _events() -> list[Event]:
    with get_session() as s:
        return s.execute(select(Event)).scalars().all()


def _payloads(event_type: str) -> list[dict]:
    return [e.payload for e in _events() if e.event_type == event_type]


def _dump() -> str:
    return json.dumps([{"t": e.event_type, "p": e.payload} for e in _events()], default=str)


# ── #1 non-user/assistant messages ──────────────────────────────────────────

def test_non_user_assistant_message_preserved(archive_home) -> None:
    """A message whose role isn't user/assistant flows through the builder's generic
    `message` path — role passed through, text kept, raw parts kept."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}},
         [("p0", {"type": "text", "text": "hi"})]),
        ("t1", {"role": "tool", "time": {"created": 1700000001000}}, [
            ("p1", {"type": "text", "text": "background note"}),
            ("p2", {"type": "diagnostic", "code": "E123", "detail": "keepme"}),
        ]),
    ])

    import_opencode_db(db)

    msgs = _payloads("message")
    assert any(p.get("role") == "tool" for p in msgs), f"role not passed through: {_dump()}"
    tool = next(p for p in msgs if p.get("role") == "tool")
    assert tool["content"] == "background note"
    # The unmodeled part survived verbatim in content_blocks.
    assert "keepme" in json.dumps(tool.get("content_blocks", [])), _dump()


# ── #2 non-text user parts ──────────────────────────────────────────────────

def test_non_text_user_parts_preserved(archive_home) -> None:
    """A file/attachment part on a user turn used to be filtered out; it must now be
    kept alongside the user text as a sibling `message` event."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}}, [
            ("p1", {"type": "text", "text": "look at this"}),
            ("p2", {"type": "file", "filename": "photo.png", "mime": "image/png", "url": "blob:xyz"}),
        ]),
    ])

    import_opencode_db(db)

    # The text still lands as a real user message.
    users = _payloads("user_message_sent")
    assert any(p.get("content") == "look at this" for p in users), _dump()
    # The file part is preserved (not dropped).
    attach = [p for p in _payloads("message") if p.get("role") == "user_attachment"]
    assert attach, f"non-text user part was dropped: {_dump()}"
    assert "photo.png" in json.dumps(attach[0].get("content_blocks", [])), _dump()


# ── #3 unknown assistant part types ─────────────────────────────────────────

def test_unknown_assistant_part_preserved(archive_home) -> None:
    """An assistant part type outside {reasoning,text,tool} became seg=None and was
    dropped; it must now surface as a `content_block` event carrying the raw part."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}},
         [("p0", {"type": "text", "text": "go"})]),
        ("a1", {"role": "assistant", "modelID": "gpt", "providerID": "oai",
                "time": {"created": 1700000001000, "completed": 1700000002000}}, [
            ("p1", {"type": "text", "text": "on it"}),
            ("p2", {"type": "snapshot", "snapshot": "snap-deadbeef", "path": "/x"}),
        ]),
    ])

    import_opencode_db(db)

    blocks = _payloads("content_block")
    assert blocks, f"unknown assistant part was dropped: {_dump()}"
    snap = next(b for b in blocks if b.get("block_type") == "snapshot")
    assert "snap-deadbeef" in json.dumps(snap.get("data", {})), _dump()
    # The modeled text part still came through normally.
    assert any(p.get("text") == "on it" for p in _payloads("text_complete")), _dump()


# ── #4 malformed JSON (message + part) ──────────────────────────────────────

def test_malformed_message_json_preserved(archive_home) -> None:
    """A message row whose data is unparseable JSON was swallowed; it must now surface
    as a visible parse_error record with the raw text retained."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}},
         [("p0", {"type": "text", "text": "ok"})]),
        ("bad", "{not valid json at all", []),
    ])

    import_opencode_db(db)

    # Surfaces via the system path as a context_summary tagged parse_error.
    errs = [p for p in _payloads("context_summary") if p.get("system_type") == "parse_error"]
    assert errs, f"malformed message JSON was silently dropped: {_dump()}"
    assert "{not valid json" in json.dumps(errs[0].get("provider_data", {})), _dump()


def test_malformed_part_json_preserved(archive_home) -> None:
    """A part row whose data is unparseable JSON was swallowed; on an assistant turn it
    must now ride the content_block path with its raw text retained."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=[
        ("u1", {"role": "user", "time": {"created": 1700000000000}},
         [("p0", {"type": "text", "text": "go"})]),
        ("a1", {"role": "assistant", "modelID": "gpt", "providerID": "oai",
                "time": {"created": 1700000001000, "completed": 1700000002000}}, [
            ("p1", {"type": "text", "text": "answer"}),
            ("p2", "}}corrupt-part-blob{{"),
        ]),
    ])

    import_opencode_db(db)

    blocks = [b for b in _payloads("content_block") if b.get("block_type") == "parse_error"]
    assert blocks, f"malformed part JSON was silently dropped: {_dump()}"
    assert "corrupt-part-blob" in json.dumps(blocks[0].get("data", {})), _dump()


# ── #5 abandoned vs live unsettled turn ─────────────────────────────────────

def _unsettled_transcript():
    return [
        ("u1", {"role": "user", "time": {"created": 1700000000000}},
         [("p0", {"type": "text", "text": "question"})]),
        # assistant turn with NO time.completed — never settled.
        ("a1", {"role": "assistant", "modelID": "gpt", "providerID": "oai",
                "time": {"created": 1700000001000}},
         [("p1", {"type": "text", "text": "partial answer"})]),
        # a later message that must not be lost with the stranded turn.
        ("u2", {"role": "user", "time": {"created": 1700000002000}},
         [("p2", {"type": "text", "text": "after context"})]),
    ]


def test_abandoned_turn_captured_when_stale(archive_home) -> None:
    """An unsettled assistant turn on a long-stale session is treated as abandoned:
    its partial content is captured (tagged incomplete) AND the message after it is
    not lost."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_STALE_TS, messages=_unsettled_transcript())

    import_opencode_db(db)

    dump = _dump()
    # The partial assistant content was captured...
    assert any(p.get("text") == "partial answer" for p in _payloads("text_complete")), dump
    # ...tagged incomplete so it's distinguishable from a settled turn...
    assert any(
        p.get("stop_reason") == "incomplete" for p in _payloads("api_request_completed")
    ), dump
    # ...and the message AFTER the abandoned turn survived (not stranded).
    assert any(p.get("content") == "after context" for p in _payloads("user_message_sent")), dump


def test_live_unsettled_turn_held_back(archive_home) -> None:
    """The settled-prefix behaviour is preserved for a fresh (live) session: an
    in-progress turn and everything after it are held back to avoid churn — they
    re-import once the turn settles on a later poll."""
    init_db()
    db = archive_home / "opencode.db"
    _make_db(db, time_updated=_FRESH_TS, messages=_unsettled_transcript())

    import_opencode_db(db)

    dump = _dump()
    # Only the settled prefix (the first user turn) imports.
    assert any(p.get("content") == "question" for p in _payloads("user_message_sent")), dump
    # The unsettled turn and its tail are held back (no churn on a live session).
    assert "partial answer" not in dump
    assert "after context" not in dump
    assert not any(
        p.get("stop_reason") == "incomplete" for p in _payloads("api_request_completed")
    ), dump
