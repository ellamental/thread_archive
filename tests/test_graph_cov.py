"""Branch coverage for ``thread_archive._knowledge.graph`` — the corpus graph projections.

Seeds the store directly (the data plane the graph projects over) and drives
every read surface, including the unavailable/empty paths.
"""

from __future__ import annotations

from thread_archive._knowledge import graph as kg
from thread_archive._store import Thread, ThreadLink, get_session, init_db


def _gid(n: int) -> str:
    """A fixed, valid ULID for graph-seed topic ``n`` (26 chars, Crockford
    alphabet, not all-digits so it is a primary-key-shaped id)."""
    return f"01TEST{n:020d}"


def _seed_graph() -> None:
    """Two triangles {1,2,3} {4,5,6} bridged 3-4, an isolated topic 7, a non-topic
    thread 99, and edges spanning every link-type family (+ a parallel edge)."""
    init_db()
    with get_session() as s:
        for tid in range(1, 8):
            s.add(Thread(id=_gid(tid), name=f"topic-{tid}", title=f"Topic {tid}", thread_type="topic"))
        s.add(Thread(id=_gid(99), name="conv-99", title="A Conversation", thread_type="conversation"))
        s.flush()
        edges = [
            (1, 2, "related"), (2, 3, "related"), (1, 3, "related"),
            (4, 5, "related"), (5, 6, "related"), (4, 6, "related"),
            (3, 4, "related"),          # bridge
            (2, 1, "related"),          # parallel undirected edge → weight accumulates
            (1, 3, "contrast"),         # oppositional → excluded from community graph
            (4, 6, "implements"),       # instantiation → down-weighted in community graph
            (5, 5, "related"),          # self-loop → skipped
            (1, 99, "related"),         # conversation endpoint → a corpus node + edge
        ]
        for src, tgt, lt in edges:
            s.add(ThreadLink(source_thread_id=_gid(src), target_thread_id=_gid(tgt), link_type=lt))
        s.commit()
    kg.reset_cache()


def test_graph_is_available_exception(archive_home, monkeypatch) -> None:
    def _boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(kg, "get_engine", _boom)
    assert kg.is_available() is False


def test_graph_status_and_metadata(archive_home) -> None:
    _seed_graph()
    status = kg.get_status()
    assert status["available"] is True
    assert status["nodes"] == 8  # 7 topics + the linked conversation
    assert status["topics"] == 7 and status["threads"] == 1
    assert status["communities"] >= 2
    # Batch metadata: a real topic resolves, an unknown id is skipped.
    meta = kg.get_topic_graph_metadata([_gid(1), _gid(999)])
    assert _gid(1) in meta and _gid(999) not in meta
    assert meta[_gid(1)]["degree"] >= 2 and meta[_gid(1)]["link_count"] == meta[_gid(1)]["degree"]
    assert kg.get_topic_graph_meta(_gid(1))["pagerank"] > 0


def test_graph_community_queries(archive_home) -> None:
    _seed_graph()
    # A topic's community members (self excluded).
    cid = kg._projection().community.get(_gid(1))
    members = kg.get_community_topic_ids(cid)
    assert _gid(1) in members
    peers = kg.get_community_peers(_gid(1))
    assert peers and all(p["thread_id"] != _gid(1) for p in peers)
    # community_members_for: known topic yields members, isolated topic yields none,
    # unknown id is skipped entirely.
    mapping = kg.community_members_for([_gid(1), _gid(7), _gid(1000)])
    assert _gid(1) in mapping and _gid(7) not in mapping and _gid(1000) not in mapping


def test_graph_bridges_and_unconnected(archive_home) -> None:
    _seed_graph()
    bridge_ids = {b["topic_id"] for b in kg.get_bridge_topics()}
    assert {_gid(3), _gid(4)} <= bridge_ids  # the bridge endpoints carry the betweenness
    unconnected = kg.get_unconnected_topics()
    assert {u["thread_id"] for u in unconnected} == {_gid(7)}


def test_graph_functions_return_empty_when_unavailable(archive_home, monkeypatch) -> None:
    _seed_graph()
    monkeypatch.setattr(kg, "is_available", lambda: False)
    assert kg._projection() is None
    assert kg.get_topic_graph_metadata([_gid(1)]) == {}
    assert kg.get_community_topic_ids(0) == []
    assert kg.get_community_peers(_gid(1)) == []
    assert kg.community_members_for([_gid(1)]) == {}
    assert kg.get_bridge_topics() == []
    assert kg.get_unconnected_topics() == []
    assert kg.get_status() == {"available": False, "error": "store is not sqlite"}


def test_graph_community_peers_unknown_thread(archive_home) -> None:
    _seed_graph()
    # A thread id not in any community → empty peers (cid None branch).
    assert kg.get_community_peers(_gid(123456)) == []


def test_graph_empty_returns_empty_results(archive_home) -> None:
    """An available-but-empty graph: no nodes, and every query returns empty
    (betweenness short-circuits on the empty graph)."""
    init_db()
    kg.reset_cache()
    assert kg.get_status()["nodes"] == 0
    assert kg.get_bridge_topics() == []
    assert kg.get_unconnected_topics() == []
    assert kg.get_topic_graph_metadata([1]) == {}
