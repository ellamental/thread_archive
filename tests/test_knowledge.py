"""The optional knowledge layer (in-process topic graph).

Isolated from the search/read core — no graph server, networkx/igraph only. The graph
is empty until topics + links exist (the core archive works without it), and it
populates as the curatorial layer writes topics/links, surviving a reindex.
"""

from __future__ import annotations

from thread_archive import _api as ta
from thread_archive import _knowledge as knowledge
from thread_archive._store import Thread, ThreadLink, get_session, init_db

# Fixed ULID ids for the seeded topic graph (index 0 unused, so edges read 1-based).
TIDS = [None] + [f"01T0PIC000000000000000000{i}" for i in range(1, 7)]


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


def test_knowledge_survives_reindex(archive_home) -> None:
    """Topics + links written via the curatorial API rebuild into a working graph on
    reindex (the projection is folded from the kg_events truth, not a snapshot)."""
    init_db()
    a = knowledge.create_topic("Auth")["topic_id"]
    b = knowledge.create_topic("Sessions")["topic_id"]
    c = knowledge.create_topic("Database")["topic_id"]
    knowledge.link_threads(a, b)
    knowledge.link_threads(b, c)

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
