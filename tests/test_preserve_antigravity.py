"""Antigravity importer must capture EVERYTHING: a step kind the importer doesn't
model is preserved as a ``message`` event, and an empty tool outcome still records
that the tool completed (and stays correctly paired) instead of being dropped.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive.importers import import_antigravity_session_incremental
from thread_archive.store import Event, get_session, init_db


def _write_jsonl(path, lines) -> None:
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def test_antigravity_preserves_unmodeled_step(archive_home) -> None:
    """A step whose source/type the importer doesn't model (here a SYSTEM
    ``SETTINGS_CHANGE``) must not be skipped as kind=None: it must be
    kept as a ``message`` event carrying the raw step verbatim."""
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>fix the bug</USER_REQUEST>"},
        # Unmodeled step — must still be kept on import.
        {"step_index": 1, "source": "SYSTEM", "type": "SETTINGS_CHANGE",
         "created_at": "2026-01-01T10:00:02Z",
         "content": "changed setting `Model Selection` from A to B."},
        {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z", "content": "On it."},
    ])

    r = import_antigravity_session_incremental(f, "ag-unmodeled")
    assert r.is_new_thread and r.events_created > 0

    with get_session() as s:
        messages = s.execute(
            select(Event.payload).where(Event.event_type == "message")
        ).scalars().all()
    assert messages, "unmodeled antigravity step was dropped instead of preserved"

    preserved = messages[0]
    assert preserved["role"] == "unknown"
    block = preserved["content_blocks"][0]
    assert block["source"] == "SYSTEM"
    assert block["step_type"] == "SETTINGS_CHANGE"
    assert block["raw"]["content"] == "changed setting `Model Selection` from A to B."

    # Idempotent: the preserved event dedups on re-import.
    n = len(_all_event_ids())
    r2 = import_antigravity_session_incremental(f, "ag-unmodeled")
    assert r2.events_created == 0
    assert len(_all_event_ids()) == n


def test_antigravity_preserves_empty_tool_outcome(archive_home) -> None:
    """An empty tool outcome step must not be dropped (that would leave its
    tool_use unpaired): it must emit a ``tool_execution_completed`` event still
    paired to the call that preceded it."""
    init_db()
    f = archive_home / "transcript.jsonl"
    _write_jsonl(f, [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
         "created_at": "2026-01-01T10:00:00Z",
         "content": "<USER_REQUEST>run it</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
         "created_at": "2026-01-01T10:00:05Z", "content": "running",
         "tool_calls": [{"name": "run_command", "args": {}}]},
        # Empty outcome for the call above — must still be kept.
        {"step_index": 2, "source": "MODEL", "type": "TOOL_RESULT",
         "created_at": "2026-01-01T10:00:06Z", "content": ""},
    ])

    r = import_antigravity_session_incremental(f, "ag-empty-outcome")
    assert r.events_created > 0

    with get_session() as s:
        results = s.execute(
            select(Event.payload).where(Event.event_type == "tool_execution_completed")
        ).scalars().all()
    assert results, "empty tool outcome was dropped instead of preserved"
    outcome = results[0]
    # Paired to the tool_use that preceded it (agtool-0), with honest empty output.
    assert outcome["tool_call_id"] == "agtool-0"
    assert outcome["tool_name"] == "run_command"
    assert outcome["output"] == ""


def _all_event_ids() -> list[int]:
    with get_session() as s:
        return [i for (i,) in s.execute(select(Event.id))]
