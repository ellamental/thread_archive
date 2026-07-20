"""In-process knowledge-graph projection over the corpus (networkx spine, Leiden engine).

A pure-graph projection, rebuildable from SQLite — no graph server. **Nodes** are the
live topics plus every thread the curated layer touches: a thread cited as evidence
(``topic_messages``) or an endpoint of a ``thread_links`` edge. Threads with no curated
contact are not nodes, and the graph is empty until curation exists — the core archive
works without this layer. **Edges** are the curated links (weight = ``strength``,
parallel links accumulate) plus one evidence edge per live-cited (topic, thread) pair,
weight ``1 + ln(citations)`` — heavily-cited pairs count more without any single pair
dominating.

Three projections over that graph:

* **PageRank / components** — every edge, undirected; ``pagerank(alpha=0.85)``.
* **Community** — drop oppositional (contrast/supersedes) + operational
  (works_on/investigates/benchmark) links, ×0.7 the instantiation links
  (implements/example-of); evidence edges ride at full weight, so the corpus shapes
  the partition rather than only the librarian's own link-drawing. Then
  :func:`thread_archive._knowledge._community.detect_communities` — Leiden, with
  seeded networkx Louvain as a fail-soft fallback.
* **Betweenness (bridges)** — hop-count (unweighted: networkx treats edge weight as
  *distance*, which would read strong links as far apart), pivot-sampled and seeded,
  so it stays deterministic and tractable at corpus scale; memoized per projection.

``degree``/``link_count`` in topic metadata — and everything the gardener reads —
count only curated topic↔topic links, deliberately narrower than the graph:
"singleton" keeps meaning "no curated link to another topic" even when evidence
connects the topic to half the corpus. The topic-facing read surfaces (communities,
peers, bridges, unconnected) emit topic nodes only; corpus nodes shape the numbers
without flooding the lists.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import networkx as nx
from sqlalchemy import text as sa_text

from .._store import get_engine, get_session
from ._community import SEED, detect_communities

logger = logging.getLogger(__name__)

# Edge-type families. The community projection excludes oppositional + operational
# edges and down-weights instantiation.
_OPPOSITIONAL = frozenset({"contrast", "supersedes"})
_OPERATIONAL = frozenset({"works_on", "investigates", "benchmark"})
_INSTANTIATION = frozenset({"implements", "example-of"})

# Betweenness pivot budget: exact below this many nodes, sampled (seeded) above.
_BETWEENNESS_PIVOTS = 256

# Lazy per-engine projection cache. {id(engine): _Projection}. Reset on rebuild.
_CACHE: dict = {}


class _Projection:
    """The computed knowledge-graph state for one engine snapshot."""

    __slots__ = ("pagerank", "community", "link_degree", "component", "members",
                 "titles", "kinds", "_pr_graph", "_kn_graph", "_betweenness")

    def __init__(self, pr_graph: "nx.Graph", kn_graph: "nx.Graph",
                 titles: dict[str, str], kinds: dict[str, str],
                 link_degree: dict[str, int]):
        self._pr_graph = pr_graph
        self._kn_graph = kn_graph
        self.titles = titles
        # node id → thread_type ('topic' for topics; the thread's own type otherwise).
        self.kinds = kinds
        # topic id → curated topic↔topic link degree (0 for evidence-only topics).
        self.link_degree = link_degree
        self.pagerank: dict[str, float] = (
            nx.pagerank(pr_graph, alpha=0.85, weight="weight") if pr_graph.number_of_nodes() else {}
        )
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
        self._betweenness: Optional[dict[str, float]] = None

    def is_topic(self, node: str) -> bool:
        return self.kinds.get(node) == "topic"

    def betweenness(self) -> dict[str, float]:
        if self._betweenness is None:
            g = self._pr_graph
            if not g.number_of_nodes():
                self._betweenness = {}
            else:
                k = min(_BETWEENNESS_PIVOTS, g.number_of_nodes())
                self._betweenness = nx.betweenness_centrality(g, k=k, seed=SEED)
        return self._betweenness


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
        corpus = {
            r[0]: (r[1], r[2]) for r in s.execute(sa_text(
                "SELECT id, title, thread_type FROM threads WHERE thread_type != 'topic'"
            ))
        }
        links = s.execute(sa_text(
            "SELECT source_thread_id, target_thread_id, link_type, "
            "COALESCE(strength, 1.0) AS w FROM thread_links"
        )).all()
        evidence = s.execute(sa_text(
            "SELECT topic_id, thread_id, COUNT(*) AS n FROM topic_messages "
            "WHERE archived_at IS NULL GROUP BY topic_id, thread_id"
        )).all()

    topic_ids = set(titles)
    kinds: dict[str, str] = {tid: "topic" for tid in topic_ids}

    def _admit(tid: str) -> bool:
        """A node may enter the graph: it is a live topic, or a known non-topic
        thread (registered on first contact). Unknown/archived-topic ids may not."""
        if tid in kinds:
            return True
        info = corpus.get(tid)
        if info is None:
            return False
        titles[tid] = info[0]
        kinds[tid] = info[1] or "conversation"
        return True

    pr_graph.add_nodes_from(topic_ids)
    kn_graph.add_nodes_from(topic_ids)
    link_graph = nx.Graph()  # curated topic↔topic links only — the gardener's degree
    link_graph.add_nodes_from(topic_ids)

    for src, tgt, link_type, w in links:
        if src == tgt or not _admit(src) or not _admit(tgt):
            continue
        weight = float(w)
        _add_weighted(pr_graph, src, tgt, weight)
        if link_type not in _OPPOSITIONAL and link_type not in _OPERATIONAL:
            _add_weighted(kn_graph, src, tgt, weight * (0.7 if link_type in _INSTANTIATION else 1.0))
        if src in topic_ids and tgt in topic_ids:
            link_graph.add_edge(src, tgt)

    for topic_id, thread_id, n in evidence:
        if topic_id not in topic_ids or topic_id == thread_id or not _admit(thread_id):
            continue
        weight = 1.0 + math.log(n)
        _add_weighted(pr_graph, topic_id, thread_id, weight)
        _add_weighted(kn_graph, topic_id, thread_id, weight)

    # A non-topic admitted for one endpoint of an edge whose other endpoint was
    # rejected may have ended up with no edge at all — drop it from the node maps.
    for stray in [tid for tid, kind in kinds.items()
                  if kind != "topic" and not pr_graph.has_node(tid)]:
        del kinds[stray]
        del titles[stray]

    link_degree = {tid: d for tid, d in link_graph.degree()}
    return _Projection(pr_graph, kn_graph, titles, kinds, link_degree)


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
    """Batch graph properties for nodes. ``pagerank`` / ``community`` come from the
    full corpus graph; ``degree`` / ``link_count`` count curated topic↔topic links
    only (0 for non-topic nodes) — the gardener's singleton semantics depend on
    that. Ids that aren't graph nodes are skipped."""
    proj = _projection()
    if proj is None or not thread_ids:
        return {}
    out: dict[str, dict] = {}
    for tid in thread_ids:
        if tid not in proj.kinds:
            continue
        deg = proj.link_degree.get(tid, 0)
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
    return [t for t in proj.members.get(community_id, []) if proj.is_topic(t)]


def get_community_peers(thread_id: str, limit: int = 5) -> list[dict]:
    """Other topics in the same community, highest-pagerank first. Corpus nodes
    shape the communities but are not emitted as peers."""
    proj = _projection()
    if proj is None:
        return []
    cid = proj.community.get(thread_id)
    if cid is None:
        return []
    peers = [t for t in proj.members.get(cid, []) if t != thread_id and proj.is_topic(t)]
    peers.sort(key=lambda t: (-proj.pagerank.get(t, 0.0), t))
    return [{"thread_id": t, "title": proj.titles.get(t), "pagerank": proj.pagerank.get(t, 0.0)}
            for t in peers[:limit]]


def community_members_for(topic_ids: list[str]) -> dict[str, list[str]]:
    """For each of ``topic_ids`` that sits in a community, its co-member topics
    (excluding itself and corpus nodes), highest-pagerank first. Topics with no
    community are omitted. Empty dict when the graph is unavailable/empty —
    callers stay graph-agnostic. Widens a set of subjects to their sibling
    subjects."""
    proj = _projection()
    if proj is None or not topic_ids:
        return {}
    out: dict[str, list[str]] = {}
    for tid in topic_ids:
        cid = proj.community.get(tid)
        if cid is None:
            continue
        members = [m for m in proj.members.get(cid, []) if m != tid and proj.is_topic(m)]
        if members:
            members.sort(key=lambda m: (-proj.pagerank.get(m, 0.0), m))
            out[tid] = members
    return out


def get_communities(limit: int = 30, member_limit: int = 8) -> list[dict]:
    """The community clusters, most-topics first — each with its highest-pagerank
    topic members and the count of corpus threads clustered with them. The
    gardener's map for promoting a cluster into a hierarchy subtree (a parent
    topic + ``part-of`` children). Communities containing no topic are omitted."""
    proj = _projection()
    if proj is None:
        return []
    clusters = []
    for cid, members in proj.members.items():
        topics = [t for t in members if proj.is_topic(t)]
        if topics:
            clusters.append((cid, topics, len(members) - len(topics)))
    clusters.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    out = []
    for cid, topics, thread_count in clusters[:limit]:
        ranked = sorted(topics, key=lambda t: (-proj.pagerank.get(t, 0.0), t))
        out.append({
            "community_id": cid,
            "size": len(topics),
            "threads": thread_count,
            "members": [{"topic_id": t, "title": proj.titles.get(t)} for t in ranked[:member_limit]],
        })
    return out


def get_bridge_topics(limit: int = 20) -> list[dict]:
    """Highest-betweenness topics — structural bridges between communities,
    measured over the full corpus graph."""
    proj = _projection()
    if proj is None:
        return []
    bc = proj.betweenness()
    ranked = [(t, sc) for t, sc in sorted(bc.items(), key=lambda kv: (-kv[1], kv[0]))
              if sc > 0 and proj.is_topic(t)][:limit]
    return [{"topic_id": t, "title": proj.titles.get(t), "score": score} for t, score in ranked]


def get_unconnected_topics(limit: int = 50) -> list[dict]:
    """Topics with no curated link to another topic — the knowledge islands.
    Evidence citations don't count: an island can be well-cited and still
    unlinked."""
    proj = _projection()
    if proj is None:
        return []
    isolated = [t for t, d in sorted(proj.link_degree.items()) if d == 0][:limit]
    return [{"thread_id": t, "link_count": 0} for t in isolated]


def get_status() -> dict:
    proj = _projection()
    if proj is None:
        return {"available": False, "error": "store is not sqlite"}
    from ._community import leiden_available

    topics = sum(1 for kind in proj.kinds.values() if kind == "topic")
    return {
        "available": True,
        "nodes": len(proj.kinds),
        "topics": topics,
        "threads": len(proj.kinds) - topics,
        "edges": proj._pr_graph.number_of_edges(),
        "communities": len(proj.members),
        "components": len(set(proj.component.values())),
        "community_engine": "leiden" if leiden_available() else "louvain",
    }
