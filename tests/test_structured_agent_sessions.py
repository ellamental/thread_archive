"""``read_thread_structured`` reports the Task-tool subagents a thread spawned.

A subagent transcript imports as its own ``thread_type='system'`` thread, soft-linked
back to its spawning session only through ``source_metadata`` (``parent_session_id`` +
``project_dir``). ``_agent_sessions_for`` reverses that link and tallies the runs by
model, feeding the viewer's reader-header "N agent sessions" line. See
``_agent_sessions_for`` in ``_retrieval/read.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval import read_thread_structured
from thread_archive._store import ImportState, Thread, init_db, mint_ulid, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, tzinfo=timezone.utc)


def _parent(s, *, source_id="proj:parent-uuid"):
    tid = mint_ulid()
    s.add(Thread(id=tid, name=f"t{tid}", title="Parent", thread_type="conversation",
                 source="claude-code", source_id=source_id,
                 inserted_at=_dt(0), updated_at=_dt(0)))
    return tid


def _subagent(s, *, parent_uuid, model, project_dir="proj"):
    tid = mint_ulid()
    s.add(Thread(
        id=tid, name=f"t{tid}", title=f"🤖 {tid}", thread_type="system",
        source="claude-code", source_id=f"{project_dir}:agent-{tid}",
        source_metadata={
            "is_subagent": True, "parent_session_id": parent_uuid,
            "project_dir": project_dir, "models": [model],
        },
        inserted_at=_dt(2), updated_at=_dt(2),
    ))
    return tid


def test_agent_sessions_tallied_by_model_most_used_first():
    init_db()
    with use_session() as s:
        p = _parent(s)
        for _ in range(3):
            _subagent(s, parent_uuid="parent-uuid", model="claude-haiku-4-5")
        for _ in range(2):
            _subagent(s, parent_uuid="parent-uuid", model="claude-opus-4-7")
        s.commit()
    agents = read_thread_structured(p)["agent_sessions"]
    assert agents["count"] == 5
    assert agents["by_model"] == [
        {"model": "claude-haiku-4-5", "count": 3},
        {"model": "claude-opus-4-7", "count": 2},
    ]


def test_no_agents_reads_as_none():
    init_db()
    with use_session() as s:
        p = _parent(s)
        s.commit()
    assert read_thread_structured(p)["agent_sessions"] is None


def test_continuation_absorbed_session_is_included():
    # A session the parent absorbed via continuation keeps its own import_state
    # watermark pointing at the parent thread — subagents it spawned must still count.
    init_db()
    with use_session() as s:
        p = _parent(s)
        s.flush()  # the import_state row's FK needs the parent thread persisted first
        s.add(ImportState(source="claude-code", source_id="proj:absorbed-uuid", thread_id=p,
                          last_line_count=0, last_file_size=0))
        _subagent(s, parent_uuid="parent-uuid", model="claude-opus-4-7")
        _subagent(s, parent_uuid="absorbed-uuid", model="claude-opus-4-7")
        s.commit()
    agents = read_thread_structured(p)["agent_sessions"]
    assert agents["count"] == 2
    assert agents["by_model"] == [{"model": "claude-opus-4-7", "count": 2}]


def test_same_uuid_in_a_different_project_is_not_counted():
    # parent_session_id is matched with project_dir, so a uuid collision across
    # projects can't attribute a foreign subagent to this thread.
    init_db()
    with use_session() as s:
        p = _parent(s)
        _subagent(s, parent_uuid="parent-uuid", model="claude-haiku-4-5")
        _subagent(s, parent_uuid="parent-uuid", model="claude-opus-4-7", project_dir="other-proj")
        s.commit()
    agents = read_thread_structured(p)["agent_sessions"]
    assert agents["count"] == 1
    assert agents["by_model"] == [{"model": "claude-haiku-4-5", "count": 1}]


def test_non_claude_code_thread_reports_no_agents():
    init_db()
    with use_session() as s:
        tid = mint_ulid()
        s.add(Thread(id=tid, name="t", title="ChatGPT chat", thread_type="conversation",
                     source="chatgpt", source_id="conv-1", inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
    assert read_thread_structured(tid)["agent_sessions"] is None
