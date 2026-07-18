"""Source fields the science / cowork importers surface rather than drop.

Claude Science: per-message ``_tokens`` land as structured token fields on
``api_request_completed`` (via the synthesized line's ``message.usage``),
``_response_id`` fills the CC parser's ``message_id`` slot, science-specific
message extras ride ``provider_data["annotations"]`` onto the anchor events, and
frame-level cost/token stats reach the thread's ``source_metadata``. Cowork:
session aggregates off the ``type: "result"`` line reach
``source_metadata["session_stats"]``. Annotation-only extras never change dedup
identity.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from thread_archive._importers import (
    import_claude_science_db,
    import_cowork_session_incremental,
)
from thread_archive._store import Event, Thread, get_session, init_db

ORG = "org-uuid-1"

_FRAME_COLS = (
    "id", "parent_frame_id", "root_frame_id", "agent_name", "status",
    "conversation_type", "name", "task_summary", "model", "project_id",
    "created_at", "updated_at", "effort", "total_cost", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_write_tokens", "aux_cost",
    "aux_input_tokens", "aux_output_tokens", "aux_cache_read_tokens",
    "aux_cache_write_tokens", "mentioned_artifact_ids",
)


def _make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE frames ("
        "id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT,"
        "agent_name TEXT, status TEXT, conversation_type TEXT, name TEXT,"
        "task_summary TEXT, model TEXT, project_id TEXT, created_at INTEGER,"
        "updated_at INTEGER, effort TEXT, total_cost REAL, input_tokens INTEGER,"
        "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,"
        "aux_cost REAL, aux_input_tokens INTEGER, aux_output_tokens INTEGER,"
        "aux_cache_read_tokens INTEGER, aux_cache_write_tokens INTEGER,"
        "mentioned_artifact_ids TEXT)"
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


_ARTIFACT_REFS = {"plot.png": {"artifact_id": "art-1", "version_id": "v-1"}}
_CELL_IMAGES = {"toolu_1": [{"sha256": "abc", "filename": "plot.png"}]}


def _science_messages(with_extras: bool) -> list[dict]:
    user = {
        "role": "user",
        "content": [{"type": "text", "text": "run the analysis"}],
        "_uuid": "u1",
    }
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "analysis done"}],
        "_uuid": "a1",
    }
    if with_extras:
        user.update({"_rolling_summary": {"id": "rs1", "text": "the story so far"},
                     "_harness_notice": True, "_harness_prompt": True,
                     "_cell_images": _CELL_IMAGES})
        assistant.update({
            "_tokens": {"input": 1200, "output": 340, "cache_read": 900,
                        "cache_write": 50, "uncached": 250},
            "_response_id": "msg_abc123",
            "_artifact_refs": _ARTIFACT_REFS,
        })
    return [user, assistant]


def _seed_science_db(path, frame_id="frame1", with_extras=True, **frame_kw) -> None:
    conn = _make_db(path)
    defaults = dict(
        id=frame_id, agent_name="OPERON", status="completed",
        conversation_type="agent", name="Analysis Run", model="claude-opus-4-8",
        project_id="proj_real", created_at=1_700_000_000_000,
        effort="high", total_cost=1.25, input_tokens=1200, output_tokens=340,
        cache_read_tokens=900, cache_write_tokens=50, aux_cost=0.05,
        aux_input_tokens=10, aux_output_tokens=20, aux_cache_read_tokens=30,
        aux_cache_write_tokens=40, mentioned_artifact_ids='["art-1", "art-2"]',
    )
    defaults.update(frame_kw)
    _add_frame(conn, **defaults)
    _add_messages(conn, frame_id, _science_messages(with_extras))
    conn.commit()
    conn.close()


def test_science_usage_message_id_and_annotations(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_science_db(db)

    scan = import_claude_science_db(db, ORG)
    assert scan.imported == 1 and scan.events_created > 0

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-science")).scalar_one()
        events = s.execute(
            select(Event).where(Event.thread_id == t.id).order_by(Event.id)
        ).scalars().all()
    by_type = {}
    for e in events:
        by_type.setdefault(e.event_type, []).append(e)

    # Per-message _tokens landed structured (and nonzero) on api_request_completed.
    completed = by_type["api_request_completed"][0].payload
    assert completed["input_tokens"] == 1200
    assert completed["output_tokens"] == 340
    assert completed["cache_read_tokens"] == 900
    assert completed["cache_write_tokens"] == 50
    assert completed["uncached_tokens"] == 250

    # Assistant-side extras rode annotations onto the assistant anchor.
    assert completed["annotations"]["artifact_refs"] == _ARTIFACT_REFS

    # User-side extras rode annotations onto user_message_sent.
    user_payload = by_type["user_message_sent"][0].payload
    ann = user_payload["annotations"]
    assert ann["rolling_summary"]["id"] == "rs1"
    assert ann["harness_notice"] is True
    assert ann["harness_prompt"] is True
    assert ann["cell_images"] == _CELL_IMAGES


def test_science_response_id_fills_message_id_slot() -> None:
    from thread_archive._importers.claude_science import _synthesize_line
    from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser

    line = _synthesize_line(
        "f1", 1, _science_messages(with_extras=True)[1], "claude-opus-4-8",
        1_700_000_000_000,
    )
    assert line["message"]["id"] == "msg_abc123"
    messages = ClaudeCodeParser().parse_export({
        "provider": "claude-code",
        "sessions": [{"session_id": "s", "project": "p", "lines": [line]}],
    })
    assert messages[0]["provider_data"]["message_id"] == "msg_abc123"


def test_science_frame_stats_reach_source_metadata(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_science_db(db)
    import_claude_science_db(db, ORG)

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-science")).scalar_one()
        meta = t.source_metadata

    assert meta["total_cost"] == 1.25
    assert meta["input_tokens"] == 1200
    assert meta["output_tokens"] == 340
    assert meta["cache_read_tokens"] == 900
    assert meta["cache_write_tokens"] == 50
    assert meta["aux_cost"] == 0.05
    assert meta["aux_input_tokens"] == 10
    assert meta["aux_output_tokens"] == 20
    assert meta["aux_cache_read_tokens"] == 30
    assert meta["aux_cache_write_tokens"] == 40
    assert meta["effort"] == "high"
    # Stored as JSON text in the DB; decoded to a list in metadata.
    assert meta["mentioned_artifact_ids"] == ["art-1", "art-2"]


def test_science_empty_stats_are_skipped(archive_home, tmp_path) -> None:
    init_db()
    db = tmp_path / "operon-cli.db"
    _seed_science_db(
        db, with_extras=False,
        effort="", total_cost=None, input_tokens=None, output_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None, aux_cost=None,
        aux_input_tokens=None, aux_output_tokens=None, aux_cache_read_tokens=None,
        aux_cache_write_tokens=None, mentioned_artifact_ids="[]",
    )
    import_claude_science_db(db, ORG)
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "claude-science")).scalar_one()
        meta = t.source_metadata
    for key in ("effort", "total_cost", "mentioned_artifact_ids", "aux_cost",
                "input_tokens", "output_tokens"):
        assert key not in meta


def test_science_old_schema_without_stat_columns_still_imports(archive_home, tmp_path) -> None:
    # A store predating the stat columns imports fine (columns selected only when present).
    init_db()
    db = tmp_path / "operon-cli.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE frames ("
        "id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT,"
        "agent_name TEXT, status TEXT, conversation_type TEXT, name TEXT,"
        "task_summary TEXT, model TEXT, project_id TEXT, created_at INTEGER,"
        "updated_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE frame_messages ("
        "frame_id TEXT, idx INTEGER, msg_json TEXT, PRIMARY KEY(frame_id, idx))"
    )
    conn.execute(
        "INSERT INTO frames (id, agent_name, status, conversation_type, name, model,"
        " project_id, created_at) VALUES ('old1', 'OPERON', 'completed', 'agent',"
        " 'Old Frame', 'claude-opus-4-8', 'proj_real', 1700000000000)"
    )
    _add_messages(conn, "old1", _science_messages(with_extras=False))
    conn.commit()
    conn.close()

    scan = import_claude_science_db(db, ORG)
    assert scan.imported == 1 and scan.failed == 0


def test_extras_leave_dedup_keys_unchanged(archive_home, tmp_path) -> None:
    # Two frames, identical content and message uuids — one bare, one with every
    # extra. Usage and annotations must not perturb the dedup identity.
    init_db()
    db = tmp_path / "operon-cli.db"
    conn = _make_db(db)
    _add_frame(conn, id="bare", agent_name="OPERON", status="completed",
               conversation_type="agent", name="Bare", model="claude-opus-4-8",
               project_id="proj_real", created_at=1_700_000_000_000)
    _add_messages(conn, "bare", _science_messages(with_extras=False))
    _add_frame(conn, id="rich", agent_name="OPERON", status="completed",
               conversation_type="agent", name="Rich", model="claude-opus-4-8",
               project_id="proj_real", created_at=1_700_000_000_000,
               total_cost=1.25)
    _add_messages(conn, "rich", _science_messages(with_extras=True))
    conn.commit()
    conn.close()

    import_claude_science_db(db, ORG)
    with get_session() as s:
        threads = {
            t.source_id: t.id for t in s.execute(
                select(Thread).where(Thread.source == "claude-science")
            ).scalars()
        }
        keys = {}
        for sid, tid in threads.items():
            keys[sid] = sorted(
                k for k in s.execute(
                    select(Event.dedup_key).where(Event.thread_id == tid)
                ).scalars() if k
            )
    assert keys[f"{ORG}:bare"] == keys[f"{ORG}:rich"]


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_cowork_result_stats_reach_source_metadata(archive_home) -> None:
    init_db()
    session_dir = archive_home / "local_stats"
    session_dir.mkdir()
    audit = session_dir / "audit.jsonl"
    usage = {"input_tokens": 15, "output_tokens": 3755,
             "cache_read_input_tokens": 178776, "cache_creation_input_tokens": 21880}
    model_usage = {"claude-opus-4-7": {"inputTokens": 15, "outputTokens": 3755,
                                       "costUSD": 0.32}}
    _write_jsonl(audit, [
        {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "do the task"}},
        {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "done"}]}},
        # A superseded mid-session result, then the current totals — last wins.
        {"type": "result", "uuid": "r0", "_audit_timestamp": "2026-01-01T10:00:06Z",
         "total_cost_usd": 0.10, "num_turns": 2, "usage": {"input_tokens": 5},
         "modelUsage": {}},
        {"type": "result", "uuid": "r1", "_audit_timestamp": "2026-01-01T10:00:10Z",
         "total_cost_usd": 0.320088, "num_turns": 5, "usage": usage,
         "modelUsage": model_usage},
    ])

    result = import_cowork_session_incremental(audit, "user:org:stats", None)
    assert result.events_created > 0

    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "cowork")).scalar_one()
        stats = t.source_metadata["session_stats"]
    assert stats["total_cost_usd"] == 0.320088
    assert stats["num_turns"] == 5
    assert stats["usage"] == usage
    assert stats["modelUsage"] == model_usage


def test_cowork_without_result_line_has_no_session_stats(archive_home) -> None:
    init_db()
    session_dir = archive_home / "local_nostats"
    session_dir.mkdir()
    audit = session_dir / "audit.jsonl"
    _write_jsonl(audit, [
        {"type": "user", "uuid": "u1", "_audit_timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "in progress"}},
        {"type": "assistant", "uuid": "a1", "_audit_timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "working"}]}},
    ])
    import_cowork_session_incremental(audit, "user:org:nostats", None)
    with get_session() as s:
        t = s.execute(select(Thread).where(Thread.source == "cowork")).scalar_one()
    assert "session_stats" not in (t.source_metadata or {})


# ── Real-data smoke (read-only against the live stores; skipped where absent) ──

# conftest redirects $HOME to a sandbox; the real home comes from passwd.
def _real_home() -> Path:
    import os
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


_REAL_SCIENCE_DBS = sorted(_real_home().glob(".claude-science/orgs/*/operon-cli.db"))


@pytest.mark.skipif(not _REAL_SCIENCE_DBS, reason="no local Claude Science store")
def test_real_science_db_has_expected_shape() -> None:
    # Read-only sanity: the live schema still carries the stat columns and the
    # per-message extras the importer maps.
    conn = sqlite3.connect(f"file:{_REAL_SCIENCE_DBS[0]}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(frames)")}
        from thread_archive._importers.claude_science import (
            _FRAME_COLUMNS,
            _FRAME_STAT_COLUMNS,
        )
        assert set(_FRAME_COLUMNS) <= cols
        assert set(_FRAME_STAT_COLUMNS) <= cols
        row = conn.execute(
            "SELECT msg_json FROM frame_messages"
            " WHERE msg_json LIKE '%\"_tokens\"%' LIMIT 1"
        ).fetchone()
        if row is not None:
            msg = json.loads(row[0])
            assert set(msg["_tokens"]) <= {
                "input", "output", "cache_read", "cache_write", "uncached"
            }
    finally:
        conn.close()
