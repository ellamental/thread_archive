"""Community detection — Leiden, with a Louvain fallback. The shared spine.

Leiden (Traag et al. 2019) strictly dominates Louvain — it guarantees
well-connected communities (Louvain can leave a community internally
disconnected) and converges to a better partition. We run it locally via
``leidenalg`` + ``python-igraph`` (base dependencies), with no graph server.

This is the one community engine in the thread family: the archive's own
corpus-native embedding graph (:mod:`.embed_graph`) partitions with it, and
the thread-librarian plugin's curated topic graph imports it from here.

:func:`detect_communities` falls back to networkx's Louvain (free with the
base ``networkx`` dependency) if those C extensions ever fail to import, so a
read path can never crash on community detection. Both paths are **seeded**
for a deterministic partition across runs.
"""

from __future__ import annotations

import logging

import networkx as nx
from networkx.algorithms.community import louvain_communities

logger = logging.getLogger(__name__)

# Seeded so a given graph yields a stable partition across calls/runs (both engines).
SEED = 42

_LEIDEN_AVAILABLE: bool | None = None


def leiden_available() -> bool:
    """Whether the Leiden engine (``leidenalg`` + ``igraph``) can be imported. They are
    base dependencies, so this is normally true; it guards the fail-soft fallback for
    the rare environment where the C extensions can't load. Memoized — probes once."""
    global _LEIDEN_AVAILABLE
    if _LEIDEN_AVAILABLE is None:
        try:
            import igraph  # noqa: F401
            import leidenalg  # noqa: F401

            _LEIDEN_AVAILABLE = True
        except Exception:
            _LEIDEN_AVAILABLE = False
    return _LEIDEN_AVAILABLE


def detect_communities(graph: "nx.Graph") -> list[list[str]]:
    """Partition ``graph`` into communities (lists of node ids), maximizing modularity.

    Uses Leiden when available, else networkx Louvain. Edge ``weight`` attributes are
    honored by both. Returns ``[]`` for an empty graph. Each community is sorted, and
    the list is order-stable, so the partition is reproducible."""
    if graph.number_of_nodes() == 0:
        return []
    if leiden_available():
        try:
            return _leiden(graph)
        except Exception:  # pragma: no cover — fall back rather than fail the read path
            logger.exception("leiden community detection failed — falling back to louvain")
    return [sorted(c) for c in louvain_communities(graph, weight="weight", seed=SEED)]


def _leiden(graph: "nx.Graph") -> list[list[str]]:
    import igraph as ig
    import leidenalg as la

    nodes = list(graph.nodes())
    index = {n: i for i, n in enumerate(nodes)}
    edges = [(index[u], index[v]) for u, v in graph.edges()]
    weights = [float(graph[u][v].get("weight", 1.0)) for u, v in graph.edges()]

    g = ig.Graph(n=len(nodes), edges=edges, directed=False)
    partition = la.find_partition(
        g, la.ModularityVertexPartition, weights=weights, seed=SEED
    )
    return [sorted(nodes[i] for i in community) for community in partition]
