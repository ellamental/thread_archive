"""Steering-message slotting — backfilled ``queued`` events render chronologically.

A steering message (typed mid-turn, queued, consumed as an attachment injection)
that is *backfilled* after its thread's original import carries a tail-end
``Event.id``. The id-ordered walk would render it at the end of the thread, so
``_slot_queued_events`` relocates events whose payload carries ``queued`` to sit
after the last event whose ``occurred_at`` is at or before their own. Everything
unmarked must stay byte-identical — bookkeeping events (``file_snapshot``) carry
inferred timestamps that are wrong by days, so a general timestamp sort would
scramble real threads (measured: ~22k inversions across ~2k threads).
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session


def _dt(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 1, 1, 10, minute, second, tzinfo=timezone.utc)


def _seed(events, tid=1):
    """events: (event_type, payload, minute) tuples; ids assigned in list order."""
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="Steer Test", thread_type="conversation",
                     source="claude-code", source_id=f"proj:{tid}",
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload, minute) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


# Turn 1 (10:01-10:04), turn 2 (10:05-10:06); the steering message was typed at
# 10:03 but backfilled last → highest id, mid-thread timestamp.
_BACKFILLED = [
    ("user_message_sent", {"content": "start the work"}, 1),
    ("text_complete", {"text": "working on it"}, 2),
    ("tool_use_complete", {"tool_name": "Bash", "input": {"command": "ls"}}, 4),
    ("text_complete", {"text": "turn one done"}, 4),
    ("user_message_sent", {"content": "next question"}, 5),
    ("text_complete", {"text": "final answer"}, 6),
    ("user_message_sent", {"content": "actually also refactor it", "queued": True}, 3),
]


def test_backfilled_steering_renders_mid_thread():
    tid = _seed(_BACKFILLED)
    out = read_thread(tid, mode="chat")
    steer = out.index("actually also refactor it")
    assert out.index("working on it") < steer < out.index("next question")


def test_backfilled_steering_in_user_mode_ordered():
    tid = _seed(_BACKFILLED)
    out = read_thread(tid, mode="user")
    assert (out.index("start the work")
            < out.index("actually also refactor it")
            < out.index("next question"))


def test_structured_view_slots_steering():
    tid = _seed(_BACKFILLED)
    msgs = read_thread_structured(tid)["messages"]
    texts = [b.get("text", "") for m in msgs for b in m["blocks"]]
    steer = texts.index("actually also refactor it")
    assert texts.index("working on it") < steer < texts.index("next question")


def test_unmarked_thread_untouched_despite_ts_inversions():
    # A file_snapshot with a garbage (days-early) inferred timestamp and a
    # late tool event with an early ts: no queued events → id order verbatim.
    events = [
        ("user_message_sent", {"content": "q one"}, 10),
        ("text_complete", {"text": "a one"}, 11),
        ("file_snapshot", {"files": []}, 1),  # inferred ts, days wrong
        ("user_message_sent", {"content": "q two"}, 12),
        ("text_complete", {"text": "a two"}, 5),  # inverted ts
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="chat")
    assert out.index("q one") < out.index("a one") < out.index("q two") < out.index("a two")


def test_garbage_ts_event_cannot_drag_steering_to_top():
    # Max-index semantics: an early-ts bookkeeping event deep in the stream must
    # not become the steering message's anchor point.
    events = [
        ("user_message_sent", {"content": "opening"}, 1),
        ("file_snapshot", {"files": []}, 0),  # garbage-early inferred ts
        ("text_complete", {"text": "reply"}, 2),
        ("user_message_sent", {"content": "steer me", "queued": True}, 1),  # ts ties opening
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="user")
    # slots after the last event with ts <= 10:01 — i.e. after "opening", not at top
    assert out.index("opening") < out.index("steer me")


def test_two_steers_keep_relative_order():
    events = [
        ("user_message_sent", {"content": "kickoff"}, 1),
        ("text_complete", {"text": "running"}, 2),
        ("user_message_sent", {"content": "final"}, 9),
        ("user_message_sent", {"content": "steer one", "queued": True}, 3),
        ("user_message_sent", {"content": "steer two", "queued": True}, 3),
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="user")
    assert (out.index("kickoff") < out.index("steer one")
            < out.index("steer two") < out.index("final"))
