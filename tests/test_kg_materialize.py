"""The knowledge materializer's fold, tested directly at ``apply_event``.

The external write package's own tests prove the fold end-to-end through the
write layer; these tests pin :func:`thread_archive._knowledge.materialize.apply_event` itself —
the exact function both the live write and the reindex replay call — against a tmp
store, using transient event objects shaped like replayed log rows:

* upsert semantics (same edge twice → one row, updated in place) with timestamps
  taken from the event, not the moment of replay;
* the tombstone + resurrect cycle on evidence;
* field filtering on link.updated (unknown fields ignored, missing row a no-op);
* merge folding a would-be self-loop away;
* the forward-compat guarantee: an unknown event type is skipped, never aborts
  a replay.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from thread_archive._knowledge.materialize import apply_event
from thread_archive._store import Thread, ThreadLink, TopicMessage, get_session, init_db

T1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def _naive(dt: datetime) -> datetime:
    """What SQLite hands back for an aware UTC datetime (tzinfo stripped)."""
    return dt.replace(tzinfo=None)


def _ev(event_type: str, payload: dict | None = None, *, entity_id: str | None = None,
        at: datetime = T1) -> SimpleNamespace:
    """A transient event, shaped like one rebuilt from a kg_events.jsonl row."""
    return SimpleNamespace(event_type=event_type, payload=payload or {}, entity_id=entity_id,
                           actor="librarian", actor_thread_id=None, occurred_at=at)


def _seed_topics(n: int = 3) -> list[int]:
    init_db()
    with get_session() as s:
        ids = []
        for i in range(n):
            t = Thread(name=f"topic-{i}", title=f"Topic {i}", thread_type="topic")
            s.add(t)
            s.flush()
            ids.append(t.id)
        s.commit()
    return ids


def test_link_created_is_an_upsert_with_event_time(archive_home) -> None:
    a, b, _ = _seed_topics()
    with get_session() as s:
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": b,
                                            "link_type": "related", "strength": 1.0}, at=T1))
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": b,
                                            "link_type": "related", "strength": 0.3}, at=T2))
        s.commit()

    with get_session() as s:
        link = s.execute(select(ThreadLink)).scalars().one()  # one row, not two
        assert link.strength == 0.3
        assert link.created_at == _naive(T1)  # made-at, from the first event
        assert link.updated_at == _naive(T2)  # not the moment of replay


def test_link_updated_filters_fields_and_ignores_missing_rows(archive_home) -> None:
    a, b, _ = _seed_topics()
    with get_session() as s:
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": b}))
        apply_event(s, _ev("link.updated", {"source_thread_id": a, "target_thread_id": b,
                                            "fields": {"strength": 0.5, "evidence": "why",
                                                       "source_thread_id": 999}}, at=T2))
        # An update for an edge that doesn't exist folds to nothing, never errors.
        apply_event(s, _ev("link.updated", {"source_thread_id": b, "target_thread_id": a,
                                            "fields": {"strength": 0.1}}))
        s.commit()

    with get_session() as s:
        link = s.execute(select(ThreadLink)).scalars().one()
        assert link.strength == 0.5 and link.evidence == "why"
        assert link.source_thread_id == a  # identity fields are not updatable


def test_evidence_tombstone_and_resurrect(archive_home) -> None:
    topic, conv, _ = _seed_topics()
    cite = {"topic_id": topic, "event_id": 41, "thread_id": conv, "quote": "the quote"}
    with get_session() as s:
        apply_event(s, _ev("evidence.added", cite, at=T1))
        apply_event(s, _ev("evidence.archived", {"topic_id": topic, "event_id": 41}, at=T2))
        s.commit()

    with get_session() as s:
        row = s.execute(select(TopicMessage)).scalars().one()
        assert row.archived_at == _naive(T2)  # stamped, not deleted

    with get_session() as s:
        apply_event(s, _ev("evidence.added", cite, at=T2))  # re-cite → resurrect, same row
        s.commit()
    with get_session() as s:
        row = s.execute(select(TopicMessage)).scalars().one()
        assert row.archived_at is None and row.quote == "the quote"


def test_merge_drops_the_self_loop_edge(archive_home) -> None:
    """Merging the far end of an edge onto its near end would create a self-loop;
    the fold drops it instead of keeping a degenerate edge."""
    a, b, _ = _seed_topics()
    with get_session() as s:
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": b}))
        apply_event(s, _ev("topic.merged", {"from_id": b, "into_id": a}))
        s.commit()

    with get_session() as s:
        assert s.execute(select(ThreadLink)).scalars().all() == []
        assert s.get(Thread, b).archived is True


def test_unknown_event_type_is_skipped_not_fatal(archive_home, caplog) -> None:
    """A replay must survive events written by a newer producer."""
    _seed_topics(1)
    with caplog.at_level("WARNING", logger="thread_archive._knowledge.materialize"):
        with get_session() as s:
            apply_event(s, _ev("topic.exploded", {"anything": 1}))
            s.commit()
    assert any("unknown event_type" in r.message for r in caplog.records)


def test_topic_lifecycle_events_project_metadata(archive_home) -> None:
    a, _, _ = _seed_topics()
    with get_session() as s:
        # created is audit-only: the write path made the thread; nothing to fold.
        apply_event(s, _ev("topic.created", {"title": "Topic 0"}, entity_id=a))
        # renamed folds title/description; an empty payload folds to nothing.
        apply_event(s, _ev("topic.renamed", {"title": "Renamed", "description": "d"}, entity_id=a))
        apply_event(s, _ev("topic.renamed", {}, entity_id=a))
        # updated filters to the allowed metadata fields.
        apply_event(s, _ev("topic.updated", {"fields": {"topic_kind": "concept", "bogus": "x"}},
                           entity_id=a))
        apply_event(s, _ev("topic.updated", {"fields": {"bogus": "x"}}, entity_id=a))
        apply_event(s, _ev("topic.archived", {}, entity_id=a))
        s.commit()

    with get_session() as s:
        row = s.get(Thread, a)
        assert row.title == "Renamed" and row.description == "d"
        assert row.topic_kind == "concept"
        assert not hasattr(row, "bogus")
        assert bool(row.archived) is True


def test_merge_repoints_and_collapses_collisions(archive_home) -> None:
    a, b, c = _seed_topics()
    with get_session() as s:
        # a-c exists on BOTH sides of the merge: repointing b's edge collides
        # and collapses instead of violating the unique key.
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": c}))
        apply_event(s, _ev("link.created", {"source_thread_id": b, "target_thread_id": c}))
        apply_event(s, _ev("evidence.added", {"topic_id": a, "event_id": 7,
                                              "thread_id": c, "quote": "q"}))
        apply_event(s, _ev("evidence.added", {"topic_id": b, "event_id": 7,
                                              "thread_id": c, "quote": "q"}))
        apply_event(s, _ev("evidence.added", {"topic_id": b, "event_id": 8,
                                              "thread_id": c, "quote": "q2"}))
        # A merge onto itself folds to nothing.
        apply_event(s, _ev("topic.merged", {"from_id": b, "into_id": b}))
        apply_event(s, _ev("topic.merged", {"from_id": b, "into_id": a}))
        s.commit()

    with get_session() as s:
        links = s.execute(select(ThreadLink)).scalars().all()
        assert len(links) == 1  # b-c collapsed into the existing a-c
        assert {links[0].source_thread_id, links[0].target_thread_id} == {a, c}
        evid = s.execute(select(TopicMessage)).scalars().all()
        # event 7 collided (kept once, on a); event 8 repointed onto a.
        assert sorted((r.topic_id, r.event_id) for r in evid) == [(a, 7), (a, 8)]
        assert bool(s.get(Thread, b).archived) is True


def test_link_deleted_removes_the_edge(archive_home) -> None:
    a, b, _ = _seed_topics()
    with get_session() as s:
        apply_event(s, _ev("link.created", {"source_thread_id": a, "target_thread_id": b}))
        apply_event(s, _ev("link.deleted", {"source_thread_id": a, "target_thread_id": b}))
        s.commit()
    with get_session() as s:
        assert s.execute(select(ThreadLink)).scalars().all() == []
