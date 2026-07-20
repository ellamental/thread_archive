"""The corpus-native embedding graph: thread communities with no curation input.

Where :mod:`thread_archive._knowledge.graph` projects the *curated* structure
(topics, links, citations), this module builds structure from the corpus
itself: every embedded thread gets a centroid (the normalized mean of its
document vectors from the shared vector pack), centroids get a cosine-kNN
graph, and Leiden partitions it (:func:`.._knowledge._community.
detect_communities` — the same seeded engine the curated graph uses). No
librarian, no gardener, no topics: the graph exists the moment the corpus is
embedded, and it covers every conversation, not just the curated slice.

Why it earns the build: on log-mined evaluation this graph's community
signal improves retrieval where the curated graph's does not —
``scripts/graph_eval.py`` is the measurement harness, run against the live
archive. Retrieval does not consume this module in the ranking path yet;
the eval is the gate for that promotion.

Reads only the vector pack (via :mod:`.vectors`' matrix cache — mmap, shared,
validity-tokened) and the ``events``/``threads`` tables. Build is ~seconds on
a ~10k-thread corpus and cached per store state; ``reset_cache`` drops it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from sqlalchemy import text as sa_text

from .._store import get_session

logger = logging.getLogger(__name__)

# Embedded pools that describe a thread's content. Deliberately the full
# embedded set: user + assistant text + the thread-meta docs.
_CTS = ("summary", "text", "title", "user")

# Neighbors per node in the kNN graph, and the similarity floor under which a
# neighbor is noise (at 768-d everything is vaguely similar; edges below the
# floor connect nothing meaningful and smear the partition).
KNN = 10
MIN_SIM = 0.55

# Batch of centroid rows per similarity matvec block (bounds peak memory:
# B x N float32).
_BLOCK = 512


@dataclass
class CorpusGraph:
    """The built graph: thread centroids + their Leiden partition."""

    thread_ids: list[str]
    centroids: np.ndarray  # (n, dim) float32, unit rows
    community: dict[str, int]
    members: dict[int, list[str]] = field(default_factory=dict)
    edges: int = 0

    _row: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._row = {t: i for i, t in enumerate(self.thread_ids)}
        for t, c in self.community.items():
            self.members.setdefault(c, []).append(t)

    def centroid(self, thread_id: str) -> Optional[np.ndarray]:
        i = self._row.get(thread_id)
        return self.centroids[i] if i is not None else None

    def similarity(self, qvec: np.ndarray, thread_ids: list[str]) -> dict[str, float]:
        """Cosine of ``qvec`` (unit-normalized here) against each thread's
        centroid; threads without a centroid are omitted."""
        q = np.asarray(qvec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0.0:
            q = q / n
        out: dict[str, float] = {}
        for t in thread_ids:
            i = self._row.get(t)
            if i is not None:
                out[t] = float(self.centroids[i] @ q)
        return out


# Process-local cache: {engine_id: (validity_token, CorpusGraph)}.
_CACHE: dict = {}


def reset_cache() -> None:
    _CACHE.clear()


def _event_threads(s) -> dict[int, str]:
    """event_id → thread_id over the non-topic corpus (topics are curated
    artifacts — this graph exists to stand without them)."""
    return {
        int(r[0]): r[1]
        for r in s.execute(sa_text(
            "SELECT e.id, e.thread_id FROM events e "
            "JOIN threads t ON t.id = e.thread_id "
            "WHERE t.thread_type != 'topic'"
        ))
    }


def build(knn: int = KNN, min_sim: float = MIN_SIM) -> Optional[CorpusGraph]:
    """Build (or serve cached) the corpus graph. ``None`` when the store has no
    embedded vectors — the graph degrades with the semantic arm, not separately."""
    import networkx as nx

    from .._knowledge._community import detect_communities
    from .._store import get_engine
    from .vectors import _load_matrix, _validity_token, ensure_index

    if not ensure_index():  # creates event_vectors when absent, like _knn does
        return None
    with get_session() as s:
        token = _validity_token(s)
    key = id(get_engine())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    ids, _ct_arr, mat, _doc_inv, _doc_rep, scope_rows = _load_matrix(_CTS)
    if len(ids) == 0:
        return None
    with get_session() as s:
        ev2thread = _event_threads(s)

    threads = sorted({ev2thread[int(e)] for e in ids if int(e) in ev2thread})
    tindex = {t: i for i, t in enumerate(threads)}
    row_thread = np.asarray(
        [tindex.get(ev2thread.get(int(e), ""), -1) for e in ids], dtype=np.int64
    )
    keep = row_thread >= 0
    if not keep.any():
        return None

    dim = mat.shape[1]
    cent = np.zeros((len(threads), dim), dtype=np.float32)
    counts = np.zeros(len(threads), dtype=np.int64)
    # The pack rows for this scope, streamed in blocks (mat is an mmap of the
    # full pack; scope_rows indexes it down to this scope's chunk rows).
    for lo in range(0, len(ids), 20000):
        hi = min(lo + 20000, len(ids))
        m = keep[lo:hi]
        if not m.any():
            continue
        block = np.asarray(mat[scope_rows[lo:hi][m]], dtype=np.float32)
        np.add.at(cent, row_thread[lo:hi][m], block)
        np.add.at(counts, row_thread[lo:hi][m], 1)
    ok = counts > 0
    norms = np.linalg.norm(cent[ok], axis=1, keepdims=True)
    cent[ok] /= np.clip(norms, 1e-9, None)

    live = np.flatnonzero(ok)
    sub = cent[live]
    g = nx.Graph()
    for lo in range(0, len(live), _BLOCK):
        sims = sub[lo:lo + _BLOCK] @ sub.T
        for r in range(sims.shape[0]):
            row = sims[r]
            row[lo + r] = -1.0  # no self-edge
            k = min(knn, len(row) - 1)
            if k <= 0:
                continue
            for j in np.argpartition(row, -k)[-k:]:
                if row[j] >= min_sim:
                    a, b = threads[live[lo + r]], threads[live[j]]
                    w = float(row[j])
                    if g.has_edge(a, b):
                        g[a][b]["weight"] = max(g[a][b]["weight"], w)
                    else:
                        g.add_edge(a, b, weight=w)

    community: dict[str, int] = {}
    for cid, comm in enumerate(detect_communities(g)):
        for node in comm:
            community[node] = cid

    graph = CorpusGraph(
        thread_ids=[threads[i] for i in live],
        centroids=sub,
        community=community,
        edges=g.number_of_edges(),
    )
    _CACHE[key] = (token, graph)
    logger.info(
        "embed_graph: %d threads, %d edges, %d communities",
        len(graph.thread_ids), graph.edges, len(graph.members),
    )
    return graph


def get_status() -> dict:
    g = build()
    if g is None:
        return {"available": False}
    return {
        "available": True,
        "threads": len(g.thread_ids),
        "edges": g.edges,
        "communities": len(g.members),
    }
