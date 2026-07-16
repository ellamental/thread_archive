"""Gardener diagnostics — the structural-health queues over the topic graph.

Read-only over the live graph: singletons, uncited topics, hierarchy gaps,
near-duplicate titles, community overview. Seeded through the curatorial write
API so the tests exercise the same event-sourced path the librarian/gardener use.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from thread_archive import _knowledge as knowledge
from thread_archive._knowledge.garden import _title_tokens
from thread_archive._store import Event, Thread, get_session, init_db


def _seed_garden() -> dict[str, int]:
    """A small graph exhibiting every issue kind.

    parent ─contains→ child (a two-node hierarchy)
    child ↔ linked (a related edge)
    island: cited but linked to nothing
    husk: no links, no citations
    dupe pair: "Thread Corp" / "thread-corp"
    """
    init_db()
    ids = {
        "parent": knowledge.create_topic("Infrastructure")["topic_id"],
        "child": knowledge.create_topic("Monitoring")["topic_id"],
        "linked": knowledge.create_topic("Alerting")["topic_id"],
        "island": knowledge.create_topic("Search Ranking")["topic_id"],
        "husk": knowledge.create_topic("Miscellany")["topic_id"],
        "dupe_a": knowledge.create_topic("Thread Corp")["topic_id"],
        "dupe_b": knowledge.create_topic("thread-corp")["topic_id"],
    }
    knowledge.link_threads(ids["parent"], ids["child"], "contains")
    knowledge.link_threads(ids["child"], ids["linked"], "related")
    # A conversation with one event so the island topic can carry a citation.
    with get_session() as s:
        conv = Thread(name="conv:1", title="a conversation", thread_type="conversation")
        s.add(conv)
        s.flush()
        ev = Event(thread_id=conv.id, stream_id="conv-1", event_type="user_message_sent",
                   payload={"content": "ranking talk"},
                   occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        s.add(ev)
        s.flush()
        conv_id, event_id = conv.id, ev.id
        s.commit()
    knowledge.add_topic_evidence(ids["island"], event_id, conv_id, "ranking talk")
    knowledge.reset_cache()
    return ids


def test_garden_status_counts(archive_home) -> None:
    ids = _seed_garden()
    st = knowledge.garden_status()
    assert st["topics"] == 7
    assert st["in_hierarchy"] == 2  # parent + child, via the contains edge
    # island, husk, and both dupes have no topic-topic link
    assert st["singletons"] == 4
    # everything except the cited island
    assert st["uncited"] == 6
    # linked has an edge but no hierarchy edge; singletons are excluded
    assert st["unparented"] == 1
    assert st["dupe_pairs"] == 1
    assert st["graph"]["available"] is True
    del ids


def test_singleton_queue_prefers_cited(archive_home) -> None:
    ids = _seed_garden()
    q = knowledge.garden_queue("singleton")
    assert {t["topic_id"] for t in q} == {ids["island"], ids["husk"], ids["dupe_a"], ids["dupe_b"]}
    # The cited island sorts first (best-evidenced islands are worth connecting).
    assert q[0]["topic_id"] == ids["island"] and q[0]["citations"] == 1


def test_uncited_queue_orders_least_linked_first(archive_home) -> None:
    ids = _seed_garden()
    q = knowledge.garden_queue("uncited")
    assert {t["topic_id"] for t in q} == {
        ids["parent"], ids["child"], ids["linked"], ids["husk"], ids["dupe_a"], ids["dupe_b"],
    }
    # Degree-0 husks (archive candidates) come before structural nodes.
    assert q[0]["degree"] == 0
    assert q[-1]["degree"] >= q[0]["degree"]


def test_unparented_queue_excludes_singletons_and_hierarchy(archive_home) -> None:
    ids = _seed_garden()
    q = knowledge.garden_queue("unparented")
    assert [t["topic_id"] for t in q] == [ids["linked"]]
    assert q[0]["degree"] == 1 and q[0]["pagerank"] > 0


def test_dupes_queue_finds_near_duplicate_titles(archive_home) -> None:
    ids = _seed_garden()
    q = knowledge.garden_queue("dupes")
    assert len(q) == 1
    pair = {q[0]["a_id"], q[0]["b_id"]}
    assert pair == {ids["dupe_a"], ids["dupe_b"]}
    assert q[0]["score"] == 1.0


def test_unknown_kind_raises(archive_home) -> None:
    init_db()
    with pytest.raises(ValueError, match="unknown kind"):
        knowledge.garden_queue("weeds")


def test_fixes_leave_their_queues(archive_home) -> None:
    """State is the data: merging the dupe and parenting the linked topic empty
    those queues on the next read."""
    ids = _seed_garden()
    knowledge.merge_topics(ids["dupe_b"], ids["dupe_a"])
    knowledge.link_threads(ids["linked"], ids["child"], "part-of")
    knowledge.reset_cache()
    assert knowledge.garden_queue("dupes") == []
    assert knowledge.garden_queue("unparented") == []
    st = knowledge.garden_status()
    assert st["topics"] == 6 and st["dupe_pairs"] == 0
    assert st["in_hierarchy"] == 3  # linked joined via its part-of edge


def test_communities_overview(archive_home) -> None:
    _seed_garden()
    comms = knowledge.get_communities(limit=5, member_limit=2)
    assert comms and comms[0]["size"] >= comms[-1]["size"]
    top = comms[0]
    assert len(top["members"]) <= 2
    assert {"topic_id", "title"} <= set(top["members"][0])


def test_title_tokens_stem_plurals() -> None:
    assert _title_tokens("frustration incidents") == _title_tokens("Frustration Incident")
    assert _title_tokens("Thread Corp") == _title_tokens("thread-corp")
