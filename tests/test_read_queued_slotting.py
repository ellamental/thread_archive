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

from sqlalchemy import select

from thread_archive._importers._events import import_lines
from thread_archive._retrieval import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session
from thread_archive._thread_import import DefaultEventBuilder
from thread_archive._thread_import.parsers.claude_code import ClaudeCodeParser


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


# ── after_event pagination over reordered turns ──────────────────────────────
# Slotting relocates a backfilled event to render mid-thread, but the event keeps
# its tail-end id. ``after_event`` must resume from the turn AFTER the one that
# *renders* that event, not from a turn chosen by numeric id order — otherwise
# resuming after a steering message walks off the end of the thread.


def test_after_event_resumes_past_backfilled_steer():
    # The steer (id 7, rendered in turn 2) must resume on turn 3, not report the
    # offset as past the end because every lower-id turn sits after it in display order.
    tid = _seed(_BACKFILLED)
    out = read_thread(tid, mode="user", after_event=7)
    assert "past the end" not in out
    assert "showing 3-3" in out
    assert "actually also refactor it" not in out  # the steer turn is behind us
    assert "next question" in out                   # its logical next turn


def test_after_event_on_ordinary_event_before_a_steer():
    # Resuming after the opening turn (event 1) still includes the steer that slots
    # into turn 2 — the reorder must not hide a turn that legitimately follows.
    tid = _seed(_BACKFILLED)
    out = read_thread(tid, mode="user", after_event=1)
    assert "showing 2-3" in out
    assert "start the work" not in out
    assert out.index("actually also refactor it") < out.index("next question")


def test_after_event_hidden_event_falls_back_to_position():
    # An id that no rendered turn carries (a hidden/lifecycle event that indexing can
    # still anchor) falls back to positional resume: after the last turn at/before it.
    tid = _seed(_BACKFILLED)
    # event 4 renders inside turn 2 ("turn one done"); id 3 (the tool_use event that
    # opens the same assistant step) is folded into that step's event_ids, so it is not
    # "hidden" — use a gap id. Ids 1..7 exist; there is no id 100, so it resolves by
    # position to the end.
    out = read_thread(tid, mode="user", after_event=100)
    assert "past the end" in out


# ── structured metadata: the span must reflect the slotted order ─────────────


def test_backfilled_steer_does_not_shift_thread_span():
    # started_at/ended_at come from the first/last event in *display* order. The steer
    # has a tail-end id but a 10:03 timestamp; it must not report itself as the end.
    tid = _seed(_BACKFILLED)
    meta = read_thread_structured(tid)
    assert meta["started_at"].endswith("10:01:00")   # the opener, not a reorder victim
    assert meta["ended_at"].endswith("10:06:00")     # the real final answer, not 10:03


# ── structured view: pin roles, boundaries, and source event ids ─────────────


def test_structured_view_role_and_event_id_sequence():
    # A flattened text-position check can't see message boundaries or provenance.
    # Pin the exact (role, event_ids) sequence: the steer is its own user message
    # (id 7) slotted between the two assistant turns, and the first assistant turn's
    # two events (tool + text, ids 3 and 4) stay grouped.
    tid = _seed(_BACKFILLED)
    msgs = read_thread_structured(tid)["messages"]
    seq = [(m["role"], m["event_ids"]) for m in msgs]
    assert seq == [
        ("user", [1]),
        ("assistant", [2]),
        ("user", [7]),
        ("assistant", [3, 4]),
        ("user", [5]),
        ("assistant", [6]),
    ]


# ── boundary table: equal / before-first / after-last / other user type ──────


def test_equal_timestamp_slots_after_tied_events():
    # Pins the documented ``<=`` (not ``<``): a steer whose timestamp ties earlier
    # events lands AFTER every tied event. Under a ``<`` mutation the steer would jump
    # ahead of "alpha"/"beta", so this ordering distinguishes the two.
    events = [
        ("user_message_sent", {"content": "alpha"}, 1),
        ("text_complete", {"text": "beta"}, 1),   # ties the steer's 10:01
        ("user_message_sent", {"content": "gamma"}, 2),
        ("user_message_sent", {"content": "steer here", "queued": True}, 1),
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="chat")
    assert (out.index("alpha") < out.index("beta")
            < out.index("steer here") < out.index("gamma"))


def test_steer_before_first_event_slots_to_top():
    # No event sits at/before the steer's timestamp → it slots to the very top.
    events = [
        ("user_message_sent", {"content": "opening"}, 5),
        ("text_complete", {"text": "reply"}, 6),
        ("user_message_sent", {"content": "early steer", "queued": True}, 1),
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="user")
    assert out.index("early steer") < out.index("opening")


def test_steer_after_last_event_slots_to_end():
    # A steer whose timestamp is later than everything lands after the last event.
    events = [
        ("user_message_sent", {"content": "kickoff"}, 1),
        ("user_message_sent", {"content": "middle"}, 2),
        ("text_complete", {"text": "done"}, 3),
        ("user_message_sent", {"content": "late steer", "queued": True}, 9),
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="user")
    assert (out.index("kickoff") < out.index("middle") < out.index("late steer"))


def test_queued_thread_message_sent_also_slots():
    # ``thread_message_sent`` is the other _USER_TYPE; a queued one slots identically.
    events = [
        ("user_message_sent", {"content": "first"}, 1),
        ("text_complete", {"text": "answer"}, 2),
        ("user_message_sent", {"content": "later"}, 5),
        ("thread_message_sent", {"content": "queued via thread_message", "queued": True}, 3),
    ]
    tid = _seed(events)
    out = read_thread(tid, mode="user")
    assert (out.index("first")
            < out.index("queued via thread_message")
            < out.index("later"))


# ── end-to-end: raw Claude Code JSONL → import → read ────────────────────────
# The unit tests above seed already-normalized events. This exercises the whole
# chain — parser tagging, event building, slotting, and reading — for a real
# ``queued_command`` attachment appended after the later conversation lines (the
# shape Claude Code writes when a steering message is backfilled).


def _cc_user(uid: str, minute: int, text: str) -> dict:
    return {"type": "user", "uuid": uid, "timestamp": _dt(minute).isoformat(),
            "message": {"role": "user", "content": text}}


def _cc_assistant(uid: str, minute: int, text: str) -> dict:
    return {"type": "assistant", "uuid": uid, "timestamp": _dt(minute).isoformat(),
            "message": {"role": "assistant", "model": "m",
                        "content": [{"type": "text", "text": text}]}}


def _cc_queued(uid: str, parent: str, line_minute: int, queued_minute: int, prompt: str) -> dict:
    # The attachment carries its own (earlier) queued timestamp; the enclosing line's
    # timestamp is late, since Claude Code writes it once the turn it interrupted ends.
    return {"type": "attachment", "uuid": uid, "parentUuid": parent,
            "timestamp": _dt(line_minute).isoformat(),
            "attachment": {"type": "queued_command", "prompt": prompt,
                           "timestamp": _dt(queued_minute).isoformat()}}


def _import_cc(lines: list[dict], tid: str = "01JAAAAAAAAAAAAAAAAAAAAAAA") -> str:
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name="cc", title="CC Steer", thread_type="conversation",
                     source="claude-code", source_id="proj:sess"))
        s.commit()
    with use_session() as s:
        import_lines(s, tid, lines, ClaudeCodeParser(), DefaultEventBuilder(),
                     source="claude-code", source_id="proj:sess")
        s.commit()
    return tid


def test_end_to_end_import_queued_command_slots_and_paginates():
    lines = [
        _cc_user("u1", 1, "start the work"),
        _cc_assistant("a1", 2, "working on it"),
        _cc_user("u2", 5, "next question"),
        _cc_assistant("a2", 6, "final answer"),
        # backfilled steer: written last (line ts 10:05), queued at 10:03
        _cc_queued("q1", "a1", line_minute=5, queued_minute=3, prompt="actually also refactor it"),
    ]
    tid = _import_cc(lines)

    with use_session() as s:
        steer = next(
            e for e in s.execute(
                select(Event).where(Event.thread_id == tid).order_by(Event.id)
            ).scalars().all()
            if e.event_type == "user_message_sent" and e.payload.get("queued")
        )
    # The attachment's own queued timestamp survived (not the late line timestamp).
    # The store returns naive UTC datetimes, so compare tz-stripped.
    assert steer.occurred_at.replace(tzinfo=None) == _dt(3).replace(tzinfo=None)
    # …and the queued tag survived the parser → builder → store round-trip.
    assert steer.payload.get("queued") is True

    # Both readers place it chronologically, mid-thread.
    out = read_thread(tid, mode="user")
    assert (out.index("start the work")
            < out.index("actually also refactor it")
            < out.index("next question"))
    msgs = read_thread_structured(tid)["messages"]
    texts = [b.get("text", "") for m in msgs for b in m["blocks"]]
    assert (texts.index("working on it")
            < texts.index("actually also refactor it")
            < texts.index("next question"))

    # after_event over the backfilled event resumes on the next logical turn.
    resumed = read_thread(tid, mode="user", after_event=steer.id)
    assert "past the end" not in resumed
    assert "actually also refactor it" not in resumed
    assert "next question" in resumed

    # The span reflects the slotted order, not the steer's tail-end id.
    meta = read_thread_structured(tid)
    assert meta["started_at"].endswith("10:01:00")
    assert meta["ended_at"].endswith("10:06:00")
