"""Community detection — Leiden, with a Louvain fallback. The shared spine.

Leiden (Traag et al. 2019) guarantees well-connected communities, which Louvain
does not, and converges to a better partition. We run it locally via
``leidenalg`` + ``python-igraph`` (the ``leiden`` extra), with no graph server.

This is the archive's community engine: the corpus-native embedding graph
(:mod:`.embed_graph`) partitions with it. It is import-safe for external
analytics layers to reuse.

:func:`detect_communities` falls back to networkx's Louvain (free with the base
``networkx`` dependency) when the extra is absent or its C extensions fail to
import, so a read path can never crash on community detection. Both paths are
**seeded** for a deterministic partition across runs.

The two engines agree on most of a corpus and differ on the modularity metric by
under a point, so the fallback is not a broad quality cliff — but where they
disagree, a whole region can partition differently and the coherence re-rank
consolidates that region's mid-list differently with it. On the gold bench that
lands as a single topic breaching its recall floor while the pooled numbers barely
move, which is why the fallback is reported (:func:`engine`) rather than trusted
to be harmless.
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
    """Whether the Leiden engine (``leidenalg`` + ``igraph``) can be imported — the
    ``leiden`` extra, present or not. Memoized: probes once, at most one log line.

    A failed probe logs at info, not warning. Absent is the correct state for a
    lexical-only install, which never builds the graph this engine partitions; whether
    it is a *fault* depends on what else is installed, and that judgment belongs to
    :func:`thread_archive.libraries`, which can see the vector arm from here."""
    global _LEIDEN_AVAILABLE
    if _LEIDEN_AVAILABLE is None:
        try:
            import igraph  # noqa: F401
            import leidenalg  # noqa: F401

            _LEIDEN_AVAILABLE = True
        except ImportError:
            _LEIDEN_AVAILABLE = False
            logger.info(
                "leidenalg/python-igraph not importable — community detection uses "
                "Louvain. Install thread-archive[leiden] for the Leiden engine."
            )
    return _LEIDEN_AVAILABLE


def engine() -> str:
    """The live community engine: ``"leiden"`` or ``"louvain"``.

    Reported by :func:`thread_archive.status` so which one is running is a fact an
    operator can read rather than infer — the module docstring covers what the
    difference costs."""
    return "leiden" if leiden_available() else "louvain"


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
    # Fail-soft like the probe above — the extra may be absent, and a caller that
    # reached here past `leiden_available()` still must not crash a read path.
    try:
        import igraph as ig
        import leidenalg as la
    except ImportError:  # pragma: no cover — the probe gates this call
        return [sorted(c) for c in louvain_communities(graph, weight="weight", seed=SEED)]

    nodes = list(graph.nodes())
    index = {n: i for i, n in enumerate(nodes)}
    edges = [(index[u], index[v]) for u, v in graph.edges()]
    weights = [float(graph[u][v].get("weight", 1.0)) for u, v in graph.edges()]

    g = ig.Graph(n=len(nodes), edges=edges, directed=False)
    partition = la.find_partition(
        g, la.ModularityVertexPartition, weights=weights, seed=SEED
    )
    return [sorted(nodes[i] for i in community) for community in partition]
