"""Grok line/session extras land as annotations + source_metadata, never dropped.

Covers the fields the modeled path would otherwise discard: assistant ``model_fingerprint``
and user ``prior_turn_interrupt`` (message-level, carried via
``provider_data["annotations"]`` onto the anchor event payload per the annotations
convention in ``thread_archive._thread_import.event_builder``), and the
``summary.json`` session fields (``reasoning_effort``, ``session_kind``,
``sandbox_profile``, ``request_id``) on the thread's ``source_metadata``.
Annotations sit outside the dedup content keys, so adding them must not change any
event's identity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from thread_archive._importers import import_grok_session_incremental
from thread_archive._importers.grok import _build_grok_messages
from thread_archive._store import Event, Thread, get_session, init_db
from thread_archive._thread_import import DefaultEventBuilder

_BASE_TS = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write_grok_session(archive_home, lines, summary=None):
    """Write a grok session fixture (``chat_history.jsonl`` + optional
    ``summary.json``) and return the chat-history path."""
    session_dir = archive_home / "grok-sess"
    session_dir.mkdir()
    path = session_dir / "chat_history.jsonl"
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")
    if summary is not None:
        (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return path


def _events() -> list[tuple[str, dict]]:
    with get_session() as s:
        return [(e.event_type, e.payload) for e in s.execute(select(Event)).scalars().all()]


def _build_all(lines) -> list[tuple[str, str, dict]]:
    """Run synthetic grok lines through the importer's message assembly and the
    shared builder; return (event_type, dedup_key, payload) triples."""
    messages = _build_grok_messages(lines, {}, {}, {}, "grok-sess")
    builder = DefaultEventBuilder()
    out: list[tuple[str, str, dict]] = []
    for message in messages:
        for event in builder.build_events(message, "s0", prev_occurred_at=_BASE_TS):
            out.append((event.event_type, event.dedup_key, event.payload))
    return out


def test_model_fingerprint_lands_on_api_request_completed(archive_home) -> None:
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
        {"type": "assistant", "content": "hello", "tool_calls": [],
         "model_fingerprint": "fp_a39489019fa99b6e"},
    ])
    r = import_grok_session_incremental(f, "grok-sess")
    assert r.is_new_thread and r.events_created > 0

    completed = [p for t, p in _events() if t == "api_request_completed"]
    assert completed, "no api_request_completed event"
    assert completed[0]["annotations"]["model_fingerprint"] == "fp_a39489019fa99b6e"


def test_model_fingerprint_on_folded_tool_result_assistant(archive_home) -> None:
    """A tool_result arriving with no open assistant turn synthesizes one; a
    fingerprint on that line still rides along as an annotation."""
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "tool_result", "tool_call_id": "call_1", "content": "ok",
         "model_fingerprint": "fp_folded"},
    ])
    r = import_grok_session_incremental(f, "grok-sess")
    assert r.events_created > 0

    completed = [p for t, p in _events() if t == "api_request_completed"]
    assert completed
    assert completed[0]["annotations"]["model_fingerprint"] == "fp_folded"


def test_prior_turn_interrupt_lands_on_user_message(archive_home) -> None:
    init_db()
    f = _write_grok_session(archive_home, [
        {"type": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
        {"type": "assistant", "content": "partial answ", "tool_calls": []},
        {"type": "user", "prior_turn_interrupt": "mid_turn_abort",
         "content": [{"type": "text", "text": "<user_query>try again</user_query>"}]},
        {"type": "assistant", "content": "full answer", "tool_calls": []},
    ])
    r = import_grok_session_incremental(f, "grok-sess")
    assert r.events_created > 0

    user_events = [p for t, p in _events() if t == "user_message_sent"]
    assert len(user_events) == 2
    interrupted = [p for p in user_events if "try again" in (p.get("content") or "")]
    assert interrupted, "interrupted user turn missing"
    assert interrupted[0]["annotations"]["prior_turn_interrupt"] == "mid_turn_abort"
    # The un-annotated genuine turn carries no annotations key at all.
    plain = [p for p in user_events if "try again" not in (p.get("content") or "")]
    assert "annotations" not in plain[0]


def test_annotation_only_extras_leave_dedup_keys_unchanged() -> None:
    """The same turns with and without the annotated extras produce identical
    dedup keys for every event — re-imports of enriched lines add nothing."""
    plain_lines = [
        {"type": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
        {"type": "assistant", "content": "hello", "tool_calls": []},
    ]
    annotated_lines = [
        {"type": "user", "prior_turn_interrupt": "mid_turn_abort",
         "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
        {"type": "assistant", "content": "hello", "tool_calls": [],
         "model_fingerprint": "fp_a39489019fa99b6e"},
    ]
    plain = _build_all(plain_lines)
    annotated = _build_all(annotated_lines)

    assert [(t, k) for t, k, _ in plain] == [(t, k) for t, k, _ in annotated]
    # And the annotated run really carries the annotations on the anchor payloads.
    by_type = {t: p for t, _, p in annotated}
    assert by_type["user_message_sent"]["annotations"] == {"prior_turn_interrupt": "mid_turn_abort"}
    assert by_type["api_request_completed"]["annotations"] == {
        "model_fingerprint": "fp_a39489019fa99b6e"
    }


def test_summary_session_fields_reach_source_metadata(archive_home) -> None:
    init_db()
    f = _write_grok_session(
        archive_home,
        [
            {"type": "user", "content": [{"type": "text", "text": "<user_query>hi</user_query>"}]},
            {"type": "assistant", "content": "hello", "tool_calls": []},
        ],
        summary={
            "info": {"id": "grok-sess", "cwd": "/tmp/w"},
            "current_model_id": "grok-4",
            "created_at": "2026-07-01T12:00:00Z",
            "reasoning_effort": "xhigh",
            "session_kind": "subagent",
            "sandbox_profile": "off",
            "request_id": "75e263df-60c5-476e-a3bc-4ed9cd887873",
            "next_trace_turn": 3,
        },
    )
    r = import_grok_session_incremental(f, "grok-sess")
    assert r.is_new_thread

    with get_session() as s:
        thread = s.execute(select(Thread)).scalars().one()
        sm = thread.source_metadata
    assert sm["reasoning_effort"] == "xhigh"
    assert sm["session_kind"] == "subagent"
    assert sm["sandbox_profile"] == "off"
    assert sm["request_id"] == "75e263df-60c5-476e-a3bc-4ed9cd887873"
    # Absent session fields stay absent (present-only, no null padding).
    assert "agent_name" not in sm
