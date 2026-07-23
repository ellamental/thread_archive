"""The relevant-subjects lens — topics as orientation over a result set.

The lens names the subjects a search's hits cluster under, so it annotates results
without touching their order. The load-bearing properties: coverage across the
result set ranks the subjects (the subject most of the hits touch leads), specificity
damps the broad stopword-like subjects (a subject linking half the corpus can't top
a narrow one it ties on coverage), it's a strict no-op when the subject graph is
empty, and it's fail-soft so it can never break search rendering.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import format_results, search
from thread_archive._retrieval import subjects as _subjects
from thread_archive._store import Event, get_session, init_db

from .kg_seed import add_topic_evidence, create_topic


def _chat(archive_home, name: str, user_text: str, day: int) -> tuple[int, int]:
    """Import a one-turn chat; return ``(event_id, thread_id)`` of its user message."""
    lines = [
        {"type": "user", "uuid": f"u{name}", "timestamp": f"2026-01-{day:02d}T10:00:00Z",
         "sessionId": name, "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": f"a{name}", "parentUuid": f"u{name}",
         "timestamp": f"2026-01-{day:02d}T10:00:05Z",
         "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "ok " + user_text}]}},
    ]
    f = archive_home / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    tid = import_session_incremental(f, f"proj:{name}").thread_id
    with get_session() as s:
        eid = s.execute(
            select(Event.id).where(Event.thread_id == tid, Event.event_type == "user_message_sent")
        ).scalar_one()
    return int(eid), tid


def _link(topic_id, ev_tid: tuple[int, int]) -> None:
    add_topic_evidence(topic_id, ev_tid[0], ev_tid[1], "cite")


def _seed(archive_home):
    """Three 'widget' chats (the result set) + three off-vocab chats that only pad a
    broad subject's corpus breadth. Narrow ⊂ result set, Broad spans result + padding."""
    init_db()
    a = _chat(archive_home, "a", "the widget alpha design", 1)
    b = _chat(archive_home, "b", "the widget beta rollout", 2)
    _chat(archive_home, "c", "the widget gamma review", 3)  # in the result set, linked to no subject
    pad = [_chat(archive_home, f"p{i}", f"unrelated kazoo topic {i}", 4 + i) for i in range(3)]

    narrow = create_topic("Narrow Subject")["topic_id"]
    broad = create_topic("Broad Subject")["topic_id"]
    solo = create_topic("Solo Subject")["topic_id"]
    for ev in (a, b):
        _link(narrow, ev)              # coverage 2, corpus breadth 2
    for ev in (a, b, *pad):
        _link(broad, ev)               # coverage 2 (a,b in results), corpus breadth 5
    _link(solo, a)                     # coverage 1
    return {"narrow": narrow, "broad": broad, "solo": solo}


def test_subjects_rank_by_coverage_then_specificity(archive_home) -> None:
    ids = _seed(archive_home)
    hits = search("widget")
    subs = _subjects.subjects_for_results(hits)
    by_id = {t: (title, chats) for t, title, chats in subs}

    # All three surface; counts are distinct result-chats touched.
    assert by_id[ids["narrow"]][1] == 2
    assert by_id[ids["broad"]][1] == 2
    assert by_id[ids["solo"]][1] == 1
    order = [t for t, _, _ in subs]
    # Narrow and Broad tie on coverage (2); the specific one leads. Both beat Solo (1).
    assert order.index(ids["narrow"]) < order.index(ids["broad"])
    assert order.index(ids["broad"]) < order.index(ids["solo"])


def test_no_subjects_without_evidence(archive_home) -> None:
    init_db()
    _chat(archive_home, "a", "the widget alpha design", 1)
    hits = search("widget")
    assert _subjects.subjects_for_results(hits) == []
    assert _subjects.format_subjects_line([]) is None


def test_lens_is_fail_soft(archive_home) -> None:
    _seed(archive_home)
    hits = search("widget")

    class _BoomSession:
        """A session whose every query explodes — in through the lens's own
        ``session`` parameter, so the real lookup hits the failure."""

        def execute(self, *a, **k):
            raise RuntimeError("subject graph exploded")

    assert _subjects.subjects_for_results(hits, session=_BoomSession()) == []  # swallowed, no raise


def test_subjects_line_carries_topic_ids() -> None:
    """Each subject shows its ``[topic <id>]`` — the lens is followable, not just
    legible: the id opens the topic page via ``thread_read(topic_id)``."""
    line = _subjects.format_subjects_line([(7, "auth flow", 3), (9, "backups", 2)])
    assert line == "  subjects: auth flow [topic 7] (3) · backups [topic 9] (2)"


def test_format_results_shows_subjects_line(archive_home, monkeypatch) -> None:
    _seed(archive_home)
    hits = search("widget")
    rendered = format_results(hits, "widget")
    assert "subjects:" in rendered
    assert "[topic " in rendered
    # …and the header teaches the follow-up moves for the ids it just showed.
    assert "open a subject: thread_read(topic_id)" in rendered

    # THREAD_ARCHIVE_SUBJECTS=0 is the kill switch — the line is omitted.
    monkeypatch.setenv("THREAD_ARCHIVE_SUBJECTS", "0")
    off = format_results(hits, "widget")
    assert "subjects:" not in off
    assert "open a subject:" not in off
