"""Grok's ``<user_query>`` wrapper is unwrapped for the readable transcript.

Grok / xAI-shaped harnesses wrap the operator's prompt in a ``<user_query>`` tag
and inject ``<user_info>`` / ``<system-reminder>`` context around it. The importer
keeps the whole turn as the event's truth (capture everything — see
``test_preserve_grok``); both readers surface just the query span so the transcript
and the web viewer don't render the raw wrapper. A turn with no query span (a
context-only injection) is rendered as-is.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thread_archive._retrieval import read_thread, read_thread_structured
from thread_archive._store import Event, Thread, init_db, use_session


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, 0, tzinfo=timezone.utc)


def _seed(events, tid=1):
    """events: (event_type, payload, minute); ids assigned in list order."""
    init_db()
    with use_session() as s:
        s.add(Thread(id=tid, name=f"t{tid}", title="Grok", thread_type="conversation",
                     source="grok", source_id=f"grok-{tid}",
                     inserted_at=_dt(0), updated_at=_dt(0)))
        s.commit()
        for i, (et, payload, minute) in enumerate(events, start=1):
            s.add(Event(id=i, thread_id=tid, stream_id="s", event_type=et,
                        payload=payload, occurred_at=_dt(minute)))
        s.commit()
    return tid


_CORPUS = [
    # A plain wrapped query (the reported case).
    ("user_message_sent", {"content": "<user_query>\nhey grok!\n</user_query>"}, 1),
    ("text_complete", {"text": "hi"}, 1),
    # A wrapped query with injected context around it — the query span wins, the
    # injected <user_info> drops out of the readable view.
    ("user_message_sent",
     {"content": "<user_info>ella, plural</user_info><user_query>what is 2+2</user_query>"}, 2),
    ("text_complete", {"text": "4"}, 2),
    # A context-only turn (no query span) is rendered unchanged, not dropped.
    ("user_message_sent", {"content": "<system-reminder>stay terse</system-reminder>"}, 3),
    ("text_complete", {"text": "ok"}, 3),
]


def test_string_reader_unwraps_user_query(archive_home) -> None:
    tid = _seed(_CORPUS)
    out = read_thread(tid, mode="user")
    assert "hey grok!" in out
    assert "<user_query>" not in out and "</user_query>" not in out
    # The query span wins over the injected context wrapper.
    assert "what is 2+2" in out
    assert "<user_info>" not in out
    # A context-only turn keeps its text (nothing to unwrap).
    assert "stay terse" in out


def test_structured_reader_unwraps_user_query(archive_home) -> None:
    tid = _seed(_CORPUS)
    res = read_thread_structured(tid, include_thinking=False, include_tools=False)
    user_texts = [b["text"] for m in res["messages"] if m["role"] == "user" for b in m["blocks"]]
    assert "hey grok!" in user_texts
    assert "what is 2+2" in user_texts
    assert not any("<user_query>" in t for t in user_texts)
    # The context-only turn survives verbatim.
    assert any("stay terse" in t for t in user_texts)


def test_unwrap_leaves_ordinary_user_text_untouched(archive_home) -> None:
    """A message that merely mentions the tag name (no real span) is not mangled."""
    tid = _seed([
        ("user_message_sent", {"content": "how do I match a <user_query> tag in regex?"}, 1),
        ("text_complete", {"text": "like so"}, 1),
    ])
    out = read_thread(tid, mode="user")
    assert "how do I match a <user_query> tag in regex?" in out
