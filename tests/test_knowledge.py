"""The optional knowledge layer (in-process topic graph).

Isolated from the search/read core — no graph server, networkx/igraph only. The graph
is empty until topics + links exist (the core archive works without it), and it
populates as the curatorial layer writes topics/links, surviving a reindex.
"""

from __future__ import annotations

from thread_archive import _api as ta
from thread_archive import _knowledge as knowledge
from thread_archive._store import Thread, ThreadLink, TopicMessage, get_session, init_db

# Fixed ULID ids for the seeded topic graph (index 0 unused, so edges read 1-based).
TIDS = [None] + [f"01T0PIC000000000000000000{i}" for i in range(1, 7)]
CIDS = [None] + [f"01C0NV0000000000000000000{i}" for i in range(1, 4)]


def _seed_two_communities() -> None:
    """Two triangles {1,2,3} and {4,5,6} joined by a single bridge edge 3↔4."""
    init_db()
    with get_session() as s:
        for i in range(1, 7):
            s.add(Thread(id=TIDS[i], name=f"topic-{i}", title=f"Topic {i}", thread_type="topic"))
        s.flush()
        edges = [(1, 2), (2, 3), (1, 3), (4, 5), (5, 6), (4, 6), (3, 4)]
        for src, tgt in edges:
            s.add(ThreadLink(source_thread_id=TIDS[src], target_thread_id=TIDS[tgt]))
        s.commit()
    knowledge.reset_cache()


def test_empty_graph(archive_home) -> None:
    init_db()
    st = ta.knowledge_status()
    assert st["available"] is True
    assert st["nodes"] == 0
    assert ta.bridge_topics() == []
    assert ta.topic_peers(1) == []


def test_communities_peers_and_bridges(archive_home) -> None:
    _seed_two_communities()

    st = knowledge.get_status()
    assert st["nodes"] == 6
    assert st["communities"] >= 2  # the two triangles split
    assert st["components"] == 1  # the bridge edge joins them

    # Topic 1's peers are in its own triangle (2 and 3), never the far cluster.
    peer_ids = {p["thread_id"] for p in knowledge.get_community_peers(TIDS[1])}
    assert peer_ids and peer_ids <= {TIDS[2], TIDS[3]}

    # The bridge endpoints (3 and 4) carry the cross-cluster betweenness.
    bridge_ids = {b["topic_id"] for b in knowledge.get_bridge_topics()}
    assert {TIDS[3], TIDS[4]} <= bridge_ids

    # Per-topic metadata is real.
    meta = knowledge.get_topic_graph_meta(TIDS[1])
    assert meta["degree"] == 2 and meta["pagerank"] > 0 and meta["community"] is not None


def _seed_corpus() -> None:
    """The two triangles plus three conversations: conv 1 heavily cited by topic 1,
    conv 2 cited once by topic 4 (with one tombstoned citation on topic 1), conv 3
    uncited and unlinked — not a graph node."""
    _seed_two_communities()
    import datetime as dt

    with get_session() as s:
        for i in range(1, 4):
            s.add(Thread(id=CIDS[i], name=f"conv-{i}", title=f"Conversation {i}",
                         thread_type="conversation"))
        s.flush()
        for eid in (101, 102, 103):  # three citations → damped, still strongest edge
            s.add(TopicMessage(topic_id=TIDS[1], event_id=eid, thread_id=CIDS[1],
                               quote="q", actor="test"))
        s.add(TopicMessage(topic_id=TIDS[4], event_id=201, thread_id=CIDS[2],
                           quote="q", actor="test"))
        s.add(TopicMessage(topic_id=TIDS[1], event_id=202, thread_id=CIDS[2],
                           quote="q", actor="test",
                           archived_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()
    knowledge.reset_cache()


def test_corpus_threads_join_the_graph(archive_home) -> None:
    _seed_corpus()

    st = knowledge.get_status()
    assert st["nodes"] == 8  # 6 topics + 2 cited conversations; conv 3 stays out
    assert st["topics"] == 6 and st["threads"] == 2
    assert st["components"] == 1

    # Evidence pulls the conversation into its topic's community…
    proj = knowledge.graph._projection()
    assert proj.community[CIDS[1]] == proj.community[TIDS[1]]
    # …with the tombstoned citation excluded: conv 2's only live edge is topic 4.
    assert set(proj._pr_graph[CIDS[2]]) == {TIDS[4]}
    # Citation counts dampen: weight is 1 + ln(n), not n.
    assert proj._pr_graph[TIDS[1]][CIDS[1]]["weight"] < 3.0

    # Conversations shape the numbers but never flood the topic-facing lists.
    for surface in (knowledge.get_community_peers(TIDS[1], limit=10),
                    ta.bridge_topics()):
        ids = {p.get("thread_id") or p.get("topic_id") for p in surface}
        assert not ids & {CIDS[1], CIDS[2]}
    for comm in knowledge.get_communities():
        assert all(m["topic_id"].startswith("01T0PIC") for m in comm["members"])
    assert sum(c["threads"] for c in knowledge.get_communities()) == 2

    # Gardener semantics hold: degree counts curated topic links only, so a
    # cited-but-unlinked topic is still a singleton — while pagerank sees evidence.
    meta = knowledge.get_topic_graph_metadata([TIDS[1], CIDS[1]])
    assert meta[TIDS[1]]["degree"] == 2  # links to topics 2 and 3, evidence uncounted
    assert meta[CIDS[1]]["degree"] == 0 and meta[CIDS[1]]["pagerank"] > 0


def test_evidence_only_topic_is_connected_but_singleton(archive_home) -> None:
    """A topic with citations and no curated link joins the graph through its
    evidence, yet stays in the singleton/unconnected queues."""
    init_db()
    with get_session() as s:
        s.add(Thread(id=TIDS[1], name="t", title="Topic", thread_type="topic"))
        s.add(Thread(id=CIDS[1], name="c", title="Conv", thread_type="conversation"))
        s.flush()
        s.add(TopicMessage(topic_id=TIDS[1], event_id=1, thread_id=CIDS[1],
                           quote="q", actor="test"))
        s.commit()
    knowledge.reset_cache()

    assert knowledge.get_status()["nodes"] == 2
    assert knowledge.get_topic_graph_meta(TIDS[1])["degree"] == 0
    assert {u["thread_id"] for u in knowledge.get_unconnected_topics()} == {TIDS[1]}


def test_knowledge_survives_reindex(archive_home) -> None:
    """Topics + links written via the curatorial API rebuild into a working graph on
    reindex (the projection is folded from the kg_events truth, not a snapshot)."""
    import pytest

    curator = pytest.importorskip("thread_librarian")  # the write surface
    init_db()
    a = curator.create_topic("Auth")["topic_id"]
    b = curator.create_topic("Sessions")["topic_id"]
    c = curator.create_topic("Database")["topic_id"]
    curator.link_threads(a, b)
    curator.link_threads(b, c)

    counts = ta.reindex()
    assert counts["threads"] == 3

    status = ta.status()
    assert status["topics"] == 3 and status["links"] == 2

    st = ta.knowledge_status()
    assert st["nodes"] == 3
    peers = ta.topic_peers(a)
    assert {p["thread_id"] for p in peers} <= {b, c}


def test_knowledge_unused_does_not_break_core(archive_home) -> None:
    """A plain conversation archive (no topics) has an empty graph and still works."""
    init_db()
    assert ta.knowledge_status()["nodes"] == 0
    # status reports zero topics/links without error
    assert ta.status()["topics"] == 0
