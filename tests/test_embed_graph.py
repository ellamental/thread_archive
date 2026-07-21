"""The corpus-native embedding graph (``thread_archive._retrieval.embed_graph``).

Small synthetic corpus: two orthogonal vector clusters → two communities,
topics excluded, similarity floor honored, cache keyed to store state — plus
the coherence re-rank's pure logic, its gamma knob, and the non-blocking
``get()`` the search path uses.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from thread_archive._retrieval import embed_graph, vectors
from thread_archive._store import Event, Thread, get_session, init_db

DIM = 768


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


def _seed(archive_home) -> dict[str, str]:
    """Four conversations in two orthogonal clusters, one topic with vectors
    (must be excluded), one conversation with no vectors (not a node)."""
    init_db()
    ids: dict[str, str] = {}
    vecs: list[tuple[int, str, np.ndarray]] = []
    with get_session() as s:
        eid = 0
        for name, ttype, axis in (
            ("a1", "conversation", 0), ("a2", "conversation", 0),
            ("b1", "conversation", 1), ("b2", "conversation", 1),
            ("top", "topic", 0), ("empty", "conversation", None),
        ):
            t = Thread(name=f"t-{name}", title=name.upper(), thread_type=ttype)
            s.add(t)
            s.flush()
            ids[name] = t.id
            if axis is not None:
                eid += 1
                s.add(Event(id=eid, thread_id=t.id, stream_id=f"st-{name}",
                            event_type="message", payload={},
                            occurred_at=datetime.now(timezone.utc)))
                vecs.append((eid, "text", _unit(axis)))
        s.commit()
    vectors.index_vectors(vecs)
    embed_graph.reset_cache()
    return ids


def test_two_clusters_two_communities_topics_out(archive_home) -> None:
    ids = _seed(archive_home)
    g = embed_graph.build()
    assert g is not None
    # The topic and the vector-less conversation are not nodes.
    assert set(g.thread_ids) == {ids["a1"], ids["a2"], ids["b1"], ids["b2"]}
    # Orthogonal clusters never link (cosine 0 < MIN_SIM): two communities,
    # each holding exactly its cluster.
    assert g.community[ids["a1"]] == g.community[ids["a2"]]
    assert g.community[ids["b1"]] == g.community[ids["b2"]]
    assert g.community[ids["a1"]] != g.community[ids["b1"]]
    assert len(g.members) == 2
    assert g.edges == 2  # one intra-cluster edge each


def test_similarity_and_centroids(archive_home) -> None:
    ids = _seed(archive_home)
    g = embed_graph.build()
    scores = g.similarity(_unit(0), [ids["a1"], ids["b1"], "missing"])
    assert scores[ids["a1"]] > 0.99
    assert abs(scores[ids["b1"]]) < 1e-6
    assert "missing" not in scores
    assert g.centroid("missing") is None


def test_cache_serves_and_resets(archive_home) -> None:
    _seed(archive_home)
    g1 = embed_graph.build()
    assert embed_graph.build() is g1  # same store state → cached object
    embed_graph.reset_cache()
    g2 = embed_graph.build()
    assert g2 is not g1 and set(g2.thread_ids) == set(g1.thread_ids)


def test_status_and_empty_store(archive_home) -> None:
    init_db()
    embed_graph.reset_cache()
    assert embed_graph.build() is None  # no vectors → no graph
    assert embed_graph.get_status() == {"available": False}
    _seed(archive_home)
    st = embed_graph.get_status()
    assert st["available"] is True and st["threads"] == 4 and st["communities"] == 2


def test_get_serves_cache_without_inline_build(archive_home) -> None:
    _seed(archive_home)
    # block=True builds inline; after that the non-blocking path serves it.
    g = embed_graph.get(block=True)
    assert g is not None
    assert embed_graph.get() is g


def test_coherence_gamma_parsing() -> None:
    assert embed_graph.coherence_gamma("") == embed_graph.COHERENCE_GAMMA
    assert embed_graph.coherence_gamma("on") == embed_graph.COHERENCE_GAMMA
    assert embed_graph.coherence_gamma("off") == 0.0
    assert embed_graph.coherence_gamma("0") == 0.0
    assert embed_graph.coherence_gamma("0.01") == 0.01
    assert embed_graph.coherence_gamma("-1") == 0.0
    assert embed_graph.coherence_gamma("banana") == 0.0


def test_apply_coherence_reorders_ranked_hits(archive_home) -> None:
    """The production wiring: with a built graph, a community-mate of the head
    thread overtakes an unaffiliated thread; hits inside a thread keep order."""
    from thread_archive import _retrieval

    ids = _seed(archive_home)
    embed_graph.get(block=True)  # cached — _apply_coherence never builds

    def hit(tid, eid):
        return {"thread_id": tid, "event_id": eid, "full_content": "x"}

    ranked = [hit(ids["a1"], 1), hit(ids["empty"], 2), hit(ids["a2"], 3), hit(ids["a2"], 4)]
    out = _retrieval._apply_coherence(list(ranked), gamma=0.05)
    tids = [h["thread_id"] for h in out]
    assert tids == [ids["a1"], ids["a2"], ids["a2"], ids["empty"]]
    assert [h["event_id"] for h in out[1:3]] == [3, 4]  # within-thread order kept
    # gamma 0 and a too-small pool are strict no-ops
    assert _retrieval._apply_coherence(list(ranked), gamma=0.0) == ranked
    assert _retrieval._apply_coherence(ranked[:2], gamma=0.05) == ranked[:2]


def test_mass_is_rank_weighted_and_normalized() -> None:
    pool = ["a", "b", "c"]
    community = {"a": 1, "b": 1, "c": 2}
    mass = embed_graph.mass_for(pool, community)
    total = 1.0 + 1 / 2 + 1 / 3
    assert abs(mass[1] - (1.0 + 1 / 2) / total) < 1e-9
    assert abs(mass[2] - (1 / 3) / total) < 1e-9
    assert embed_graph.mass_for([], community) == {}


def test_coherence_boosts_community_mates() -> None:
    # d sits last but shares the dominant community; a large gamma must lift it
    # above the unaffiliated c, and gamma=0 must preserve production order.
    pool = ["a", "b", "c", "d"]
    community = {"a": 1, "b": 1, "d": 1}
    assert embed_graph.coherence_order(pool, community, 0.0) == pool
    order = embed_graph.coherence_order(pool, community, 0.05)
    assert order.index("d") < order.index("c")
