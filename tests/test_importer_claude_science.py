"""Claude Science import: a per-org ``operon-cli.db`` of conversation **frames**
imports under ``source='claude-science'`` — root frames as conversation threads,
child frames as hidden 🤖 subagent threads, the per-project uploads container and the
shipped ``proj_example`` demo excluded, and re-scans idempotent / incremental.
"""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import func, select

from thread_archive._importers import import_claude_science_db
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._watcher import ClaudeScienceWatcher, discover_claude_science_dbs

ORG = "org-uuid-1"

_FRAME_COLS = (
    "id", "parent_frame_id", "root_frame_id", "agent_name", "status",
    "conversation_type", "name", "task_summary", "model", "project_id",
    "created_at", "updated_at",
)


def _make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE frames ("
        "id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT, agent_name TEXT,"
        "status TEXT, conversation_type TEXT, name TEXT, task_summary TEXT, model TEXT,"
        "project_id TEXT, created_at INTEGER, updated_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE frame_messages ("
        "frame_id TEXT, idx INTEGER, msg_json TEXT, PRIMARY KEY(frame_id, idx))"
    )
    return conn


def _add_frame(conn, **kw) -> None:
    row = {c: kw.get(c) for c in _FRAME_COLS}
    cols = ", ".join(_FRAME_COLS)
    ph = ", ".join("?" for _ in _FRAME_COLS)
    conn.execute(f"INSERT INTO frames ({cols}) VALUES ({ph})", [row[c] for c in _FRAME_COLS])


def _add_messages(conn, frame_id, messages) -> None:
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [(frame_id, i, json.dumps(m)) for i, m in enumerate(messages)],
    )


def _user(text, uuid):
    return {"role": "user", "content": [{"type": "text", "text": text}], "_uuid": uuid}


def _assistant(text, uuid):
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "_uuid": uuid}


def _seed_db(path) -> None:
    conn = _make_db(path)
    # A real root conversation.
    _add_frame(conn, id="root1", agent_name="OPERON", status="completed",
               conversation_type="agent", name="Explore Capabilities",
               model="claude-opus-4-8", project_id="proj_real", created_at=1_700_000_000_000)
    _add_messages(conn, "root1", [_user("what can i do here?", "u1"),
                                  _assistant("lots of science", "a1")])
    # A subagent (child) frame → hidden system thread.
    _add_frame(conn, id="child1", parent_frame_id="root1", root_frame_id="root1",
               agent_name="REVIEWER", status="completed", conversation_type="agent",
               task_summary="Review the work", model="claude-opus-4-8",
               project_id="proj_real", created_at=1_700_000_100_000)
    _add_messages(conn, "child1", [_user("review this", "u2"),
                                   _assistant("looks good", "a2")])
    # The per-project uploads container — not a conversation, excluded.
    _add_frame(conn, id="uploads1", agent_name="UPLOADS", status="completed",
               conversation_type="uploads", name="User Uploads",
               project_id="proj_real", created_at=1_700_000_000_000)
    # Anthropic's shipped demo project — excluded.
    _add_frame(conn, id="demo1", agent_name="OPERON", status="completed",
               conversation_type="agent", name="CRISPR Demo",
               project_id="proj_example", created_at=1_700_000_000_000)
    _add_messages(conn, "demo1", [_user("demo question", "ud"),
                                  _assistant("demo answer", "ad")])
    conn.commit()
    conn.close()


def test_imports_root_and_subagent_excludes_uploads_and_demo(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_db(db)

    scan = import_claude_science_db(db, ORG)
    # Two importable frames (root + child); uploads has no messages, demo is filtered.
    assert scan.processed == 2
    assert scan.imported == 2
    assert scan.events_created > 0

    with get_session() as s:
        threads = s.execute(
            select(Thread).where(Thread.source == "claude-science").order_by(Thread.id)
        ).scalars().all()
        by_sid = {t.source_id: t for t in threads}

    # The shipped demo and the uploads container never become threads.
    assert f"{ORG}:demo1" not in by_sid
    assert f"{ORG}:uploads1" not in by_sid

    root = by_sid[f"{ORG}:root1"]
    assert root.thread_type == "conversation"
    assert root.title == "Explore Capabilities"
    assert root.source_metadata["org_uuid"] == ORG
    assert root.source_metadata["agent_name"] == "OPERON"
    assert root.source_metadata.get("is_subagent") is None

    child = by_sid[f"{ORG}:child1"]
    assert child.thread_type == "system"
    assert child.title.startswith("🤖")
    assert child.source_metadata["is_subagent"] is True
    assert child.source_metadata["parent_frame_id"] == "root1"


def test_reimport_is_noop(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_db(db)

    import_claude_science_db(db, ORG)
    with get_session() as s:
        n1 = s.execute(select(func.count(Event.id))).scalar_one()

    again = import_claude_science_db(db, ORG)
    assert again.events_created == 0
    with get_session() as s:
        assert s.execute(select(func.count(Event.id))).scalar_one() == n1


def test_incremental_append_imports_only_new(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_db(db)
    import_claude_science_db(db, ORG)
    with get_session() as s:
        n1 = s.execute(select(func.count(Event.id))).scalar_one()

    # The root conversation grows by a turn.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [("root1", 2, json.dumps(_user("a follow-up", "u3"))),
         ("root1", 3, json.dumps(_assistant("a reply", "a3")))],
    )
    conn.commit()
    conn.close()

    scan = import_claude_science_db(db, ORG)
    assert scan.events_created > 0
    with get_session() as s:
        n2 = s.execute(select(func.count(Event.id))).scalar_one()
        # Exactly the new turn's events, no re-import of the old ones.
        followup = s.execute(
            select(func.count(Event.id)).where(
                func.json_extract(Event.payload, "$.content") == "a follow-up"
            )
        ).scalar_one()
    assert n2 > n1
    assert followup == 1


def test_incremental_pass_survives_filtered_rows(archive_home, tmp_path) -> None:
    """A store row the importer filters (a non-user/assistant role, undecodable
    JSON) must not shift the incremental slice: the watermark counts raw rows, so
    the slice must too — sliced against the filtered list, every filtered row
    would silently drop one of the next pass's newest messages, permanently."""
    init_db()
    db = tmp_path / "operon-cli.db"
    conn = _make_db(db)
    _add_frame(conn, id="root1", agent_name="OPERON", status="completed",
               conversation_type="agent", name="Filtered Rows",
               model="claude-opus-4-8", project_id="proj_real",
               created_at=1_700_000_000_000)
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [("root1", 0, json.dumps(_user("hello", "u1"))),
         ("root1", 1, json.dumps({"role": "system",
                                  "content": [{"type": "text", "text": "injected"}]})),
         ("root1", 2, json.dumps(_assistant("hi there", "a1"))),
         ("root1", 3, "{not json")],
    )
    conn.commit()
    conn.close()
    import_claude_science_db(db, ORG)

    # The conversation grows by a turn after the watermark landed.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
        [("root1", 4, json.dumps(_user("newest question", "u2"))),
         ("root1", 5, json.dumps(_assistant("newest answer", "a2")))],
    )
    conn.commit()
    conn.close()

    scan = import_claude_science_db(db, ORG)
    assert scan.events_created > 0
    with get_session() as s:
        # User content lands at $.content, assistant text at text_complete's $.text.
        for key, text in (("$.content", "newest question"), ("$.text", "newest answer")):
            n = s.execute(
                select(func.count(Event.id)).where(
                    func.json_extract(Event.payload, key) == text
                )
            ).scalar_one()
            assert n == 1, f"lost across the filtered-row watermark: {text!r}"


def test_watcher_discovers_mtime_gates_and_self_gates(archive_home, tmp_path) -> None:
    init_db()
    base = tmp_path / "orgs"
    org_dir = base / ORG
    org_dir.mkdir(parents=True)
    db = org_dir / "operon-cli.db"
    _seed_db(db)

    assert discover_claude_science_dbs(base) == [(db, ORG)]
    w = ClaudeScienceWatcher(base=base)
    assert w.is_available()

    r1 = w.poll()
    assert r1.items_imported == 2 and r1.events_created > 0
    with get_session() as s:
        n1 = s.execute(select(func.count(Event.id))).scalar_one()

    # Unchanged DB → mtime gate skips it wholesale.
    r2 = w.poll()
    assert r2.events_created == 0
    with get_session() as s:
        assert s.execute(select(func.count(Event.id))).scalar_one() == n1

    # No store → inert, process-free.
    assert not ClaudeScienceWatcher(base=tmp_path / "nope").is_available()


def test_watcher_in_default_set() -> None:
    from thread_archive._watcher.sources import default_watchers

    assert "claude-science" in [w.source_name for w in default_watchers()]
