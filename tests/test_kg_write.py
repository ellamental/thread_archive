"""The event-sourced curatorial write layer: write → fold → projection, and the
reindex replay that rebuilds the projection from ``kg_events.jsonl``.

Every knowledge-layer mutation is a ``KgEvent`` appended to the truth log and folded
into the ``thread_links`` / ``topic_messages`` projections. These tests prove the fold
is correct and idempotent, that tombstones (unlink / archive) preserve history, that a
``rm index.db && reindex`` rebuilds the projection purely from the log, and — the
transition guarantee — that the replay composes a legacy snapshot *seed* with event
*deltas* (including deleting a seeded row).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

import thread_archive as ta
from thread_archive.knowledge import (
    add_topic_evidence,
    archive_topic,
    create_topic,
    link_threads,
    merge_topics,
    review_queue,
    thread_user_messages,
    topic_search,
    unlink_threads,
)
from thread_archive.knowledge._community import detect_communities, leiden_available
from thread_archive.store import Event, KgEvent, Thread, ThreadLink, TopicMessage, get_session
from thread_archive.truth.jsonl_log import KG_EVENTS_FILE

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_conversation(source_id: str = "sess-1", content: str = "hello world") -> tuple[int, int]:
    """A conversation thread with one user message; returns (thread_id, event_id)."""
    with get_session() as s:
        t = Thread(
            name=f"claude-code:{source_id}", title=source_id, thread_type="conversation",
            source="claude-code", source_id=source_id,
        )
        s.add(t)
        s.flush()
        e = Event(
            thread_id=t.id, stream_id=source_id, event_type="user_message_sent",
            payload={"content": content}, occurred_at=NOW,
        )
        s.add(e)
        s.flush()
        ids = (t.id, e.id)
        s.commit()
    return ids


def _kg_lines(home) -> list[dict]:
    path = home / "truth" / KG_EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _links() -> set[tuple[int, int, str]]:
    with get_session() as s:
        return {
            (link.source_thread_id, link.target_thread_id, link.link_type)
            for link in s.execute(select(ThreadLink)).scalars()
        }


# ── create / log ──────────────────────────────────────────────────────────────
def test_create_topic_writes_thread_and_event_log(archive_home):
    ta.open_archive()
    r = create_topic("Auth", "authentication concerns")
    assert r["topic_id"] > 0

    with get_session() as s:
        topic = s.get(Thread, r["topic_id"])
        assert topic.thread_type == "topic" and topic.title == "Auth"
        events = s.execute(select(KgEvent)).scalars().all()
        assert [e.event_type for e in events] == ["topic.created"]
        assert events[0].entity_id == str(r["topic_id"])

    lines = _kg_lines(archive_home)
    assert lines and lines[0]["type"] == "kg_event" and lines[0]["event_type"] == "topic.created"


def test_create_topic_rejects_duplicate_title(archive_home):
    ta.open_archive()
    create_topic("Sessions")
    with pytest.raises(ValueError):
        create_topic("Sessions")


# ── links: upsert + idempotency ────────────────────────────────────────────────
def test_link_is_upsert_and_idempotent(archive_home):
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]

    link_threads(a, b, "related", strength=1.0)
    link_threads(a, b, "related", strength=0.4)  # same edge → update, not a 2nd row

    assert _links() == {(a, b, "related")}
    with get_session() as s:
        link = s.execute(select(ThreadLink)).scalars().one()
        assert link.strength == pytest.approx(0.4)
    # the topic graph sees both nodes
    assert ta.knowledge_status()["nodes"] == 2


def test_unlink_is_a_tombstone(archive_home):
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    link_threads(a, b)
    unlink_threads(a, b)

    assert _links() == set()
    with get_session() as s:
        types = [e.event_type for e in s.execute(select(KgEvent).order_by(KgEvent.id)).scalars()]
    # the full operation history survives — create + delete, never silent loss
    assert types == ["topic.created", "topic.created", "link.created", "link.deleted"]


# ── evidence + review flow ──────────────────────────────────────────────────────
def test_evidence_and_review_queue(archive_home):
    ta.open_archive()
    conv, eid = _seed_conversation("sess-A")
    topic = create_topic("Topic X")["topic_id"]

    # a freshly-imported conversation with no citations is unreviewed — in the queue
    assert any(row["id"] == conv for row in review_queue())

    add_topic_evidence(topic, eid, conv, "the salient quote")
    add_topic_evidence(topic, eid, conv, "re-run, idempotent")  # same (topic,event) → no 2nd row

    with get_session() as s:
        rows = s.execute(select(TopicMessage)).scalars().all()
        assert len(rows) == 1 and rows[0].topic_id == topic and rows[0].event_id == eid

    # citing a message from the conversation marks it reviewed → it leaves the queue
    assert all(row["id"] != conv for row in review_queue())


def test_review_queue_excludes_own_session(archive_home):
    ta.open_archive()
    conv, _ = _seed_conversation("sess-self")
    assert any(r["id"] == conv for r in review_queue())
    assert all(r["id"] != conv for r in review_queue(exclude_source_id="sess-self"))


def test_curation_read_helpers(archive_home):
    ta.open_archive()
    conv, eid = _seed_conversation("sess-read", content="find me later")
    create_topic("Findable Topic")

    assert {t["title"] for t in topic_search("findable")} == {"Findable Topic"}
    msgs = thread_user_messages(conv)
    assert msgs == [{"event_id": eid, "text": "find me later"}]


# ── merge ───────────────────────────────────────────────────────────────────────
def test_merge_repoints_and_archives(archive_home):
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    keep = create_topic("Keep")["topic_id"]
    gone = create_topic("Gone")["topic_id"]
    conv, eid = _seed_conversation("sess-merge")

    link_threads(a, gone, "related")      # → should repoint to keep
    link_threads(keep, b, "related")
    add_topic_evidence(gone, eid, conv, "cite")  # → should repoint to keep

    merge_topics(gone, keep)

    assert _links() == {(a, keep, "related"), (keep, b, "related")}
    with get_session() as s:
        gone_t = s.get(Thread, gone)
        assert gone_t.archived is True
        tm = s.execute(select(TopicMessage)).scalars().one()
        assert tm.topic_id == keep


# ── reindex replay (the round-trip) ─────────────────────────────────────────────
def test_reindex_rebuilds_projection_from_kg_events(archive_home):
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    c = create_topic("C")["topic_id"]
    link_threads(a, b, "related")
    link_threads(b, c, "related")
    unlink_threads(a, b, "related")  # tombstone

    counts = ta.reindex()
    # 3 topic.created + 2 link.created + 1 link.deleted
    assert counts["kg_events"] == 6

    assert _links() == {(b, c, "related")}  # a→b was tombstoned in the log
    with get_session() as s:
        n_topics = s.execute(
            select(func.count()).select_from(Thread).where(Thread.thread_type == "topic")
        ).scalar()
        assert n_topics == 3


def test_reindex_composes_snapshot_seed_with_event_deltas(archive_home):
    """A legacy snapshot link (no event) seeds reindex; event deltas fold on top —
    including a tombstone that removes the seeded link. This is the snapshot→log
    transition guarantee: the librarian's events reconcile the backfill's snapshots."""
    ta.open_archive()
    a = create_topic("A")["topic_id"]
    b = create_topic("B")["topic_id"]
    c = create_topic("C")["topic_id"]

    # Seed link L=(a,b) directly into the table (no kg_event), then checkpoint so it
    # lands in the thread_links.jsonl snapshot — exactly the backfill's shape.
    with get_session() as s:
        s.add(ThreadLink(source_thread_id=a, target_thread_id=b, link_type="related"))
        s.commit()
    ta.checkpoint()
    assert (archive_home / "truth" / "thread_links.jsonl").exists()

    # Librarian deltas: add M=(b,c) and delete the seeded L=(a,b). Don't checkpoint
    # again, so the snapshot still holds the stale L — the replay must reconcile it.
    link_threads(b, c, "related")
    unlink_threads(a, b, "related")

    ta.reindex()
    assert _links() == {(b, c, "related")}  # seed L removed by the tombstone, delta M present


# ── Leiden community detection ──────────────────────────────────────────────────
def test_community_detection_partitions_two_triangles(archive_home):
    ta.open_archive()
    ids = [create_topic(f"T{i}")["topic_id"] for i in range(6)]
    # two triangles joined by a single bridge edge
    triangles = [(0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5), (2, 3)]
    for x, y in triangles:
        link_threads(ids[x], ids[y], "related")

    comms = detect_communities  # sanity: callable
    assert comms is not None

    st = ta.knowledge_status()
    assert st["nodes"] == 6
    assert st["communities"] >= 2
    assert st["community_engine"] in ("leiden", "louvain")


def test_leiden_is_the_engine(archive_home):
    # leidenalg + igraph are base dependencies; Leiden should be the live engine.
    assert leiden_available() is True
    ta.open_archive()
    create_topic("solo")
    assert ta.knowledge_status()["community_engine"] == "leiden"
