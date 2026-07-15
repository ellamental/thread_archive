"""The topic-bridged recall arm — subjects raise recall of chats FTS can't reach.

The arm follows the lexical/vector seed pool through the ``topic_messages`` links
to the *other* conversations evidenced under the same subject, and fuses them in.
The load-bearing property: a conversation that shares a subject with a hit but
shares none of the query's words — invisible to FTS — becomes reachable. It stays
a strict no-op when the subject graph is empty and fail-soft on any error, so it
can never break lexical search.
"""

from __future__ import annotations

from sqlalchemy import select

from thread_archive import _knowledge as knowledge
from thread_archive._importers import import_session_incremental
from thread_archive._retrieval import search
from thread_archive._retrieval import topical as _topical
from thread_archive._store import Event, get_session, init_db


def _write_turn(path, uid, aid, user_text, asst_text, day):
    import json

    lines = [
        {"type": "user", "uuid": uid, "timestamp": f"2026-01-{day:02d}T10:00:00Z",
         "sessionId": "s", "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "uuid": aid, "timestamp": f"2026-01-{day:02d}T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [{"type": "text", "text": asst_text}]}},
    ]
    path.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n", encoding="utf-8")


def _seed_two_unrelated_chats(archive_home):
    """Two conversations with DISJOINT vocabulary: a query hits one, never the other."""
    init_db()
    fa = archive_home / "a.jsonl"
    _write_turn(fa, "ua", "aa", "the aardvark migration plan for winter",
                "Aardvarks migrate south along the ridge.", 1)
    import_session_incremental(fa, "proj:a")

    fb = archive_home / "b.jsonl"
    _write_turn(fb, "ub", "ab", "flibbertigibbet quixotic zephyr protocol",
                "The zephyr protocol handshake completes in three steps.", 2)
    import_session_incremental(fb, "proj:b")


def _first_hit(query):
    hits = search(query, topical=False)
    assert hits, f"expected a lexical hit for {query!r}"
    return hits[0]["event_id"], hits[0]["thread_id"]


def test_subject_bridges_a_chat_fts_cannot_reach(archive_home) -> None:
    _seed_two_unrelated_chats(archive_home)

    a_event, a_thread = _first_hit("aardvark")
    b_event, b_thread = _first_hit("flibbertigibbet")
    assert a_thread != b_thread

    # Baseline: "aardvark" shares no word with chat B, so FTS never surfaces it.
    base_threads = {h["thread_id"] for h in search("aardvark", topical=False)}
    assert b_thread not in base_threads

    # Link both chats to one subject, then the arm bridges A → subject → B.
    topic = knowledge.create_topic("Field Notes")
    tid = topic["topic_id"]
    knowledge.add_topic_evidence(tid, a_event, a_thread, "aardvark migration")
    knowledge.add_topic_evidence(tid, b_event, b_thread, "zephyr protocol")
    knowledge.reset_cache()

    hits = search("aardvark", topical=True)
    by_thread = {h["thread_id"]: h for h in hits}
    assert b_thread in by_thread, "the subject-linked chat should be recalled"
    assert a_thread in by_thread, "the lexical hit is still present"
    # The bridged hit carries its provenance — it came in through the subject graph.
    assert by_thread[b_thread].get("_topical", 0.0) > 0.0


def test_empty_subject_graph_is_a_noop(archive_home) -> None:
    """With no topics/evidence, arm-on and arm-off return byte-identical results."""
    _seed_two_unrelated_chats(archive_home)
    on = [(h["event_id"], h["thread_id"]) for h in search("aardvark", topical=True)]
    off = [(h["event_id"], h["thread_id"]) for h in search("aardvark", topical=False)]
    assert on == off


def test_arm_is_fail_soft(archive_home, monkeypatch) -> None:
    """A crash inside the arm never breaks lexical search."""
    _seed_two_unrelated_chats(archive_home)
    a_event, a_thread = _first_hit("aardvark")
    topic = knowledge.create_topic("Field Notes")
    knowledge.add_topic_evidence(topic["topic_id"], a_event, a_thread, "q")
    knowledge.reset_cache()

    def _boom(*a, **k):
        raise RuntimeError("subject graph exploded")

    monkeypatch.setattr(_topical, "_subject_weights_from_seed", _boom)
    hits = search("aardvark", topical=True)
    assert any(h["thread_id"] == a_thread for h in hits), "lexical search survives an arm failure"


def test_evidence_respects_content_type_scope(archive_home) -> None:
    """The arm honors the content-type filter — a user-scoped search can't be
    widened past its scope by subject-linked assistant text."""
    _seed_two_unrelated_chats(archive_home)
    a_event, a_thread = _first_hit("aardvark")
    _, b_thread = _first_hit("zephyr")
    assert a_thread != b_thread

    # Cite chat B's ASSISTANT text (content_type 'text'), not a user message.
    with get_session() as s:
        b_text = s.execute(
            select(Event.id, Event.thread_id)
            .where(Event.event_type == "text_complete", Event.thread_id == b_thread)
        ).first()
    assert b_text is not None
    b_event, b_thread = b_text

    topic = knowledge.create_topic("Field Notes")
    tid = topic["topic_id"]
    knowledge.add_topic_evidence(tid, a_event, a_thread, "a")
    knowledge.add_topic_evidence(tid, b_event, b_thread, "b")
    knowledge.reset_cache()

    # Scope to user messages: the assistant-text evidence is out of scope, so the
    # arm must not surface it.
    user_scoped = {h["thread_id"] for h in search("aardvark", content_types=["user"], topical=True)}
    assert b_thread not in user_scoped
