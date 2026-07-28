"""The corpus-native embedding graph: thread communities with no topic-graph input.

Every embedded thread gets a centroid (the normalized mean of its document
vectors from the shared vector pack), centroids get a cosine-kNN graph, and
Leiden partitions it (:func:`.community.detect_communities`, the seeded shared
spine). No topic-graph input, no topics: the graph exists the moment
the corpus is embedded, and it covers every conversation — this is the
corpus-wide structure the topic graph cannot see.

**The coherence re-rank is the production consumer.** Within a ranked search
pool, threads whose community carries more of the pool's top mass get a small
additive boost (:func:`coherence_order`). It is a light mid-list orderer, not a
headline mover: on the log-mined click protocol (187 cases, full production
pool) the shipped gamma lifts success and recall at depth while costing a
little at rank 1 — baseline → 0.005: S@5 0.401→0.428, S@10 0.513→0.519,
recall@10 0.417→0.426, S@1 0.203→0.193, MRR flat at 0.298. It consolidates the
mid-list around the query's community; it does not improve the top hit. Those
labels are click labels, censored by the incumbent ranker, so read the harness
as a regression check rather than as evidence of a gain. The same signal
computed from the topic graph loses on the identical cases, which is why the
topic graph stays out of ranking. ``search_lab/graph_eval.py`` is the harness.

``THREAD_ARCHIVE_COHERENCE`` tunes it per process: unset/``on`` uses the
default gamma, ``off``/``0`` disables, a float overrides gamma.

**The search path never pays the build.** A build is ~seconds on a ~10k-thread
corpus and the cache key is the store's validity token, which moves with
ingest — so :func:`get` serves the cached graph (stale is fine for a
community prior) and refreshes in a background single-flight thread;
until the first build lands it returns ``None`` and the coherence boost
no-ops. ``get(block=True)`` builds inline (eval, warm pass, tests).

**The cache outlives the process** (:mod:`.graph_cache`). Held only in memory, it
starts empty at every restart, and until the first build lands the re-rank stands
down — so the same query returns a different order for the first several seconds
of a process, with nothing in the output to say so. A restart now serves the
persisted graph immediately (stale, refreshing behind it, exactly as a long-lived
process does) and skips the rebuild outright when the store has not moved.

Reads only the vector pack (via :mod:`.vectors`' matrix cache — mmap, shared,
validity-tokened) and the ``events``/``threads`` tables. ``reset_cache``
drops the cache (reindex calls it via the vectors reset path).
"""

from __future__ import annotations

import logging
import os
import threading
import time
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

# Coherence re-rank defaults: how many pool-head threads vote on community
# mass, the RRF base constant, and the default gamma — the middle of the swept
# 0.002–0.01 range, where the log-mined eval puts the S@5 and S@20 optima. The
# sweep does not resolve one value: 0.01 reads better at S@10 and recall@10,
# and the whole spread is a case or two on a 187-case protocol.
TOP_MASS = 10
RRF_K = 60
COHERENCE_GAMMA = 0.005

_ENV = "THREAD_ARCHIVE_COHERENCE"


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


# Process-local cache: {engine_id: (validity_token, CorpusGraph | None)}.
# None is cached too — a store with no embedded vectors shouldn't re-probe on
# every search, only when the token moves.
_CACHE: dict = {}
# Engines with a background refresh thread in flight — the guard against spawning
# a second one. It does not cover the build itself: `get(block=True)` builds on the
# calling thread and would otherwise race a background refresh, so the work is
# guarded separately below.
_REFRESHING: set[int] = set()
_REFRESH_LOCK = threading.Lock()
# One build at a time per engine, whoever asked. The two entry points — the warm
# pass building inline and the search path kicking a background refresh — otherwise
# run the identical build concurrently, which is exactly what happens at startup:
# the warm pass is ~15s into its build when the first search finds an empty cache.
# The loser waits for the winner's result instead of duplicating it, which costs it
# nothing (it was going to wait out a build either way) and halves the CPU and the
# peak memory of a corpus-wide Leiden partition.
_BUILDING: set[int] = set()
_BUILD_LOCKS: dict[int, threading.Lock] = {}
_BUILD_GUARD = threading.Lock()
# Probe the validity token (a count scan) and kick a refresh at most this often: the
# graph is a coarse community prior, so a minute of staleness is immaterial, and
# without the gate every search during continuous ingest re-probes and rebuilds.
_REFRESH_COOLDOWN_S = 60.0
_checked_at: dict = {}  # {engine id: monotonic ts of the last staleness probe}


def reset_cache() -> None:
    """Drop this process's graph cache. Memory only — the persisted copy is keyed
    by the store's own token and invalidated at its source
    (:func:`.graph_cache.drop`), so dropping it here would throw away a valid
    cache that costs a rebuild to recreate."""
    _CACHE.clear()
    _checked_at.clear()


def _cache_params(knn: int, min_sim: float) -> dict:
    """The build's shape, recorded with a persisted graph and required to match
    before one is served. Everything that changes the partition for an unchanged
    corpus belongs here — including the community engine, since Leiden and the
    Louvain fallback disagree on some regions and each other's graphs are not
    interchangeable."""
    from . import graph_cache
    from .community import engine

    return {
        "format": graph_cache.FORMAT,
        "knn": int(knn),
        "min_sim": float(min_sim),
        "engine": engine(),
        "cts": list(_CTS),
    }


def _disk_entry(knn: int = KNN, min_sim: float = MIN_SIM) -> Optional[tuple]:
    """The persisted graph as a cache entry ``(token, graph)``, or None.

    The stored tag carries only the token's cross-process half; the in-process
    write counter is re-read live, so a process that has written vectors since
    startup sees the entry as stale (its own writes may have changed rows the
    tag cannot see) while a pure reader sees it as current.
    """
    from . import graph_cache, vectors

    hit = graph_cache.load(_cache_params(knn, min_sim))
    if hit is None:
        return None
    (count, rowid), graph = hit
    return ((vectors._write_version, count, rowid), graph)


def coherence_gamma(env: str | None = None) -> float:
    """The configured coherence boost: 0.0 disables. Unset/``on``/``auto`` use
    the swept default; ``off``/``0`` disable; a float overrides. ``env``
    overrides the environment lookup (tests inject)."""
    raw = (os.environ.get(_ENV, "") if env is None else env).strip().lower()
    if raw in ("", "on", "auto"):
        return COHERENCE_GAMMA
    if raw in ("off", "false", "no"):
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a gamma; coherence stays at default off", _ENV, raw)
        return 0.0
    return max(0.0, val)


def mass_for(pool: list[str], community: dict[str, int], top: int = TOP_MASS) -> dict[int, float]:
    """Rank-weighted share of the pool's top slots each community holds — pure."""
    mass: dict[int, float] = {}
    total = 0.0
    for i, t in enumerate(pool[:top]):
        w = 1.0 / (i + 1)
        total += w
        cid = community.get(t)
        if cid is not None:
            mass[cid] = mass.get(cid, 0.0) + w
    return {c: m / total for c, m in mass.items()} if total else {}


def coherence_order(pool: list[str], community: dict[str, int], gamma: float) -> list[str]:
    """Re-rank a thread pool by RRF base + gamma * its community's mass — pure.
    The eval-proven formula: ``score = 1/(60+rank) + gamma * community_mass``."""
    mass = mass_for(pool, community)
    base = {t: 1.0 / (RRF_K + r) for r, t in enumerate(pool, start=1)}
    return sorted(pool, key=lambda t: (-(base[t] + gamma * mass.get(community.get(t, -1), 0.0)), t))


def _event_threads(s) -> dict[int, str]:
    """event_id → thread_id over the non-topic corpus (topics are separate
    artifacts — this graph exists to stand without them)."""
    return {
        int(r[0]): r[1]
        for r in s.execute(sa_text(
            "SELECT e.id, e.thread_id FROM events e "
            "JOIN threads t ON t.id = e.thread_id "
            "WHERE t.thread_type != 'topic'"
        ))
    }


def get(*, block: bool = False) -> Optional[CorpusGraph]:
    """The corpus graph for consumers that must not pay build latency.

    Serves the cached graph immediately — stale is acceptable for a community
    prior — and, when nothing is cached or the store has moved past the cached
    validity token, kicks a single-flight background rebuild. The token probe (a
    count scan) and the rebuild fire at most once per :data:`_REFRESH_COOLDOWN_S`,
    so continuous ingest can't make every search re-probe. Returns ``None`` until
    the first build lands (the coherence boost simply no-ops until then).
    ``block=True`` builds inline (warm pass, eval, tests)."""
    if block:
        return build()
    from .._store import get_engine

    key = id(get_engine())
    cached = _CACHE.get(key)
    if cached is None:
        # Nothing in memory: the first touch of a fresh process. A persisted graph
        # is served without waiting for its token to be checked — serving stale is
        # already this function's contract, and the alternative on offer is not a
        # fresher graph but none at all.
        cached = _disk_entry()
        if cached is None:
            _refresh_async(key)  # nothing to serve yet — get the first build going
            return None
        _CACHE[key] = cached
    now = time.monotonic()
    if now - _checked_at.get(key, 0.0) >= _REFRESH_COOLDOWN_S:
        _checked_at[key] = now
        stale = True
        try:
            from .vectors import _validity_token

            with get_session() as s:
                stale = cached[0] != _validity_token(s)
        except Exception:  # noqa: BLE001 — a staleness probe must never break search
            stale = False
        if stale:
            _refresh_async(key)
    return cached[1]


def is_refreshing() -> bool:
    """Whether a graph build is in flight in this process — read by the contention
    sample. A build is measured in seconds (Leiden over the whole corpus), so a
    search running beside one is not competing for nothing.

    Reads the build state rather than the background-thread guard: an inline build
    (the warm pass) competes with a concurrent search exactly as much as a
    backgrounded one does, and a contention signal that only sees one of them
    under-reports the case with the worst timing — startup."""
    return bool(_BUILDING)


def _refresh_async(key: int) -> None:
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def _run() -> None:
        try:
            build()
        except Exception:  # noqa: BLE001 — background refresh is best-effort
            logger.exception("corpus graph background refresh failed")
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=_run, name="embed-graph-refresh", daemon=True).start()


def build(knn: int = KNN, min_sim: float = MIN_SIM) -> Optional[CorpusGraph]:
    """Build (or serve cached) the corpus graph. ``None`` when the store has no
    embedded vectors — the graph degrades with the semantic arm, not separately."""
    from .._store import get_engine
    from .vectors import _validity_token, ensure_index

    if not ensure_index():  # creates event_vectors when absent, like _knn does
        return None
    with get_session() as s:
        token = _validity_token(s)
    key = id(get_engine())
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    with _build_lock(key):
        # Re-checked under the lock: whoever we queued behind was building this
        # same token, so their result is ours and the build we were about to do is
        # already done. This is the whole point of the lock — not serializing
        # builds, but making the second one unnecessary.
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == token:
            return cached[1]
        # Some earlier process already built this exact token and wrote it down.
        # Required to match here (unlike `get`, which serves stale): this is the
        # authoritative path, and callers of `build` are asking for the graph of
        # the store as it stands.
        entry = _disk_entry(knn, min_sim)
        if entry is not None and entry[0] == token:
            _CACHE[key] = entry
            return entry[1]
        with _BUILD_GUARD:
            _BUILDING.add(key)
        try:
            return _build_graph(key, token, knn, min_sim)
        finally:
            with _BUILD_GUARD:
                _BUILDING.discard(key)


def _build_lock(key: int) -> threading.Lock:
    """The per-engine build lock, created on first use."""
    with _BUILD_GUARD:
        lock = _BUILD_LOCKS.get(key)
        if lock is None:
            lock = _BUILD_LOCKS[key] = threading.Lock()
        return lock


def _build_graph(key: int, token: object, knn: int, min_sim: float) -> Optional[CorpusGraph]:
    """The build proper — always called holding this engine's build lock, with the
    cache already checked against ``token``."""
    import networkx as nx

    from .community import detect_communities
    from .vectors import _build_matrix_entry

    # The authoritative build reads the live matrix directly (the pure builder),
    # not the search path's serve-stale cache — the graph must reflect the store
    # it just tokened, and this call is already gated by the graph token cache above.
    _tok, ids, _ct_arr, mat, _doc_inv, _doc_rep, scope_rows, _ts = _build_matrix_entry(_CTS)
    if len(ids) == 0:
        _CACHE[key] = (token, None)  # no vectors: don't re-probe until the store moves
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
        _CACHE[key] = (token, None)
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
    from . import graph_cache

    graph_cache.save(token, graph, _cache_params(knn, min_sim))
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
