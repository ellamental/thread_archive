"""In-process topic-graph projection (networkx spine, optional Leiden community engine).

A pure-graph topic projection. The graph is rebuildable from ``thread_links`` + topic
``threads``, so there is no graph server: build a networkx graph from SQLite and run
the algorithms in process.

Two projections:
  * **PageRank** — every link, undirected, weight = ``strength``; ``pagerank(alpha=0.85)``.
  * **Community** — drop oppositional (contrast/supersedes) + operational
    (works_on/investigates/benchmark) edges, ×0.7 the instantiation edges
    (implements/example-of); then :func:`thread_archive._knowledge._community.detect_communities`
    — Leiden (the algorithm Neo4j GDS used; a base dependency), with networkx Louvain
    as a seeded, deterministic fail-soft fallback.

Topics are threads with ``thread_type='topic'``; the graph is populated as the
librarian curates (or by a migration of existing topics + links). The graph is empty
(and all queries return empty) until then — by design (the core archive works without
this layer).
"""

from __future__ import annotations

import logging
from typing import Optional

import networkx as nx
from sqlalchemy import text as sa_text

from .._store import get_engine, get_session
from ._community import detect_communities

logger = logging.getLogger(__name__)

# Edge-type families. The community projection excludes oppositional + operational
# edges and down-weights instantiation.
_OPPOSITIONAL = frozenset({"contrast", "supersedes"})
_OPERATIONAL = frozenset({"works_on", "investigates", "benchmark"})
_INSTANTIATION = frozenset({"implements", "example-of"})

# Lazy per-engine projection cache. {id(engine): _Projection}. Reset on rebuild.
_CACHE: dict = {}


class _Projection:
    """The computed topic-graph state for one engine snapshot."""

    __slots__ = ("pagerank", "community", "degree", "component", "members", "titles",
                 "_pr_graph", "_kn_graph")

    def __init__(self, pr_graph: "nx.Graph", kn_graph: "nx.Graph", titles: dict[str, str]):
        self._pr_graph = pr_graph
        self._kn_graph = kn_graph
        self.titles = titles
        self.pagerank: dict[str, float] = (
            nx.pagerank(pr_graph, alpha=0.85, weight="weight") if pr_graph.number_of_nodes() else {}
        )
        self.degree: dict[str, int] = dict(pr_graph.degree())
        self.component: dict[str, int] = {}
        for cid, comp in enumerate(nx.connected_components(pr_graph)):
            for n in comp:
                self.component[n] = cid
        self.community: dict[str, int] = {}
        self.members: dict[int, list[str]] = {}
        for cid, comm in enumerate(detect_communities(kn_graph)):
            self.members[cid] = sorted(comm)
            for n in comm:
                self.community[n] = cid

    def betweenness(self) -> dict[str, float]:
        if not self._pr_graph.number_of_nodes():
            return {}
        return nx.betweenness_centrality(self._pr_graph, weight="weight")


def is_available() -> bool:
    try:
        return get_engine().dialect.name == "sqlite"
    except Exception:
        return False


def reset_cache() -> None:
    """Drop the cached projection (call after the KG mutates / on rebuild)."""
    _CACHE.clear()


def _build_projection() -> _Projection:
    pr_graph = nx.Graph()
    kn_graph = nx.Graph()
    with get_session() as s:
        titles = {
            r[0]: r[1] for r in s.execute(sa_text(
                "SELECT id, title FROM threads WHERE thread_type = 'topic' "
                "AND (archived IS NULL OR archived = 0)"
            ))
        }
        links = s.execute(sa_text(
            "SELECT source_thread_id, target_thread_id, link_type, "
            "COALESCE(strength, 1.0) AS w FROM thread_links"
        )).all()

    topic_ids = set(titles)
    pr_graph.add_nodes_from(topic_ids)
    kn_graph.add_nodes_from(topic_ids)
    for src, tgt, link_type, w in links:
        if src == tgt or src not in topic_ids or tgt not in topic_ids:
            continue
        weight = float(w)
        _add_weighted(pr_graph, src, tgt, weight)
        if link_type not in _OPPOSITIONAL and link_type not in _OPERATIONAL:
            _add_weighted(kn_graph, src, tgt, weight * (0.7 if link_type in _INSTANTIATION else 1.0))
    return _Projection(pr_graph, kn_graph, titles)


def _add_weighted(g: "nx.Graph", a: str, b: str, w: float) -> None:
    if g.has_edge(a, b):
        g[a][b]["weight"] += w
    else:
        g.add_edge(a, b, weight=w)


def _projection() -> Optional[_Projection]:
    if not is_available():
        return None
    key = id(get_engine())
    proj = _CACHE.get(key)
    if proj is None:
        proj = _build_projection()
        _CACHE[key] = proj
    return proj


def get_topic_graph_metadata(thread_ids: list[str]) -> dict[str, dict]:
    """Batch graph properties (pagerank / community / degree / link_count) for topics."""
    proj = _projection()
    if proj is None or not thread_ids:
        return {}
    out: dict[str, dict] = {}
    for tid in thread_ids:
        if tid not in proj.degree and tid not in proj.pagerank:
            continue
        deg = proj.degree.get(tid, 0)
        out[tid] = {
            "pagerank": proj.pagerank.get(tid, 0.0),
            "community": proj.community.get(tid),
            "degree": deg,
            "link_count": deg,
        }
    return out


def get_topic_graph_meta(thread_id: str) -> dict | None:
    return get_topic_graph_metadata([thread_id]).get(thread_id)


def get_community_topic_ids(community_id: int) -> list[str]:
    proj = _projection()
    if proj is None:
        return []
    return list(proj.members.get(community_id, []))


def get_community_peers(thread_id: str, limit: int = 5) -> list[dict]:
    """Other topics in the same community, highest-pagerank first."""
    proj = _projection()
    if proj is None:
        return []
    cid = proj.community.get(thread_id)
    if cid is None:
        return []
    peers = [t for t in proj.members.get(cid, []) if t != thread_id]
    peers.sort(key=lambda t: (-proj.pagerank.get(t, 0.0), t))
    return [{"thread_id": t, "title": proj.titles.get(t), "pagerank": proj.pagerank.get(t, 0.0)}
            for t in peers[:limit]]


def community_members_for(topic_ids: list[str]) -> dict[str, list[str]]:
    """For each of ``topic_ids`` that sits in a community, its co-member topics
    (excluding itself), highest-pagerank first. Topics with no community are
    omitted. Empty dict when the graph is unavailable/empty — callers stay
    graph-agnostic. The retrieval topic arm uses this to widen the subjects a
    query matched to their sibling subjects."""
    proj = _projection()
    if proj is None or not topic_ids:
        return {}
    out: dict[str, list[str]] = {}
    for tid in topic_ids:
        cid = proj.community.get(tid)
        if cid is None:
            continue
        members = [m for m in proj.members.get(cid, []) if m != tid]
        if members:
            members.sort(key=lambda m: (-proj.pagerank.get(m, 0.0), m))
            out[tid] = members
    return out


def get_communities(limit: int = 30, member_limit: int = 8) -> list[dict]:
    """The community clusters, largest first — each with its highest-pagerank
    members. The gardener's map for promoting a cluster into a hierarchy subtree
    (a parent topic + ``part-of`` children)."""
    proj = _projection()
    if proj is None:
        return []
    out = []
    for cid, members in sorted(proj.members.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]:
        ranked = sorted(members, key=lambda t: (-proj.pagerank.get(t, 0.0), t))
        out.append({
            "community_id": cid,
            "size": len(members),
            "members": [{"topic_id": t, "title": proj.titles.get(t)} for t in ranked[:member_limit]],
        })
    return out


def get_bridge_topics(limit: int = 20) -> list[dict]:
    """Highest-betweenness topics — structural bridges between communities."""
    proj = _projection()
    if proj is None:
        return []
    bc = proj.betweenness()
    ranked = [(t, sc) for t, sc in sorted(bc.items(), key=lambda kv: (-kv[1], kv[0])) if sc > 0][:limit]
    return [{"topic_id": t, "title": proj.titles.get(t), "score": score} for t, score in ranked]


def get_unconnected_topics(limit: int = 50) -> list[dict]:
    """Topics with no link — the knowledge islands."""
    proj = _projection()
    if proj is None:
        return []
    isolated = [t for t, d in proj.degree.items() if d == 0][:limit]
    return [{"thread_id": t, "link_count": 0} for t in isolated]


def get_status() -> dict:
    proj = _projection()
    if proj is None:
        return {"available": False, "error": "store is not sqlite"}
    from ._community import leiden_available

    return {
        "available": True,
        "nodes": len(proj.degree),
        "communities": len(proj.members),
        "components": len(set(proj.component.values())),
        "community_engine": "leiden" if leiden_available() else "louvain",
    }
