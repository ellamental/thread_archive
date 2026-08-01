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
headline mover: it consolidates the mid-list around the query's community and
does not lift the top hit. Read :data:`COHERENCE_GAMMA` as *inherited and not
currently re-derived* — it comes from a protocol ``search_lab/README.md``
describes under "What a number here is worth", and nothing that runs today can
re-derive it. The topic graph stays out
of ranking for a reason that does not depend on any of that: it sees only the
conversations somebody curated a topic for, where this graph covers every
embedded thread.

``THREAD_ARCHIVE_COHERENCE`` tunes it per process: unset/``on`` uses the
default gamma, ``off``/``0`` disables, a float overrides gamma.

**The search path never pays the build.** A build is ~seconds on a ~10k-thread
corpus and the cache key is the store's validity token, which moves with
ingest — so :func:`get` serves the cached graph (stale is fine for a
community prior) and refreshes in a background single-flight thread;
until the first build lands it returns ``None`` and the coherence boost
no-ops. ``get(block=True)`` builds inline against the current token (eval, tests);
:func:`warm` is the starting-server door, which serves the persisted graph and
builds only when there is none. :func:`rebuild_floor_s` bounds how often the
*machine* rebuilds, across processes — the token moves with every ingest pass, so
without it each of several concurrent processes rebuilds the same partition.

**The cache outlives the process** (:mod:`.graph_cache`). A restart serves the
persisted graph immediately — stale, refreshing behind it, exactly as a long-lived
process does — and skips the rebuild outright when the store has not moved. What
that buys is the window it closes: a process with no graph stands the re-rank
down, so the same query comes back in a different order with nothing in the
output to say so.

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
from typing import TYPE_CHECKING, Optional

import numpy as np
from sqlalchemy import text as sa_text

from .._store import current_archive, current_archive_or_none, get_session

if TYPE_CHECKING:
    from .._store import Archive

logger = logging.getLogger(__name__)

# The content-type pools a thread's centroid is built from — exactly the pools
# :mod:`.vectors` embeds, since a type nothing embeds contributes no rows and only
# invalidates every persisted graph when it is added or removed (it rides the build
# shape, :func:`_cache_params`).
_CTS = ("text", "title", "user")

# Neighbors per node in the kNN graph, and the similarity floor under which a
# neighbor is noise (at 768-d everything is vaguely similar; edges below the
# floor connect nothing meaningful and smear the partition).
KNN = 10
MIN_SIM = 0.55

# Batch of centroid rows per similarity matvec block (bounds peak memory:
# B x N float32).
_BLOCK = 512

# Coherence re-rank defaults: how many pool-head threads vote on community mass,
# the RRF base constant, and the default gamma. Gamma sits in the middle of a
# 0.002–0.01 range that no measurement ever resolved to a single value, and the
# protocol it came from is retired — see the module docstring on what standing it
# has. What keeps it safe is its scale, not its provenance: it is added to an RRF
# base of 1/(60+rank), so it reorders inside the mid-list rather than rewriting
# the head.
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


# The graph cache, held by the open archive (``Archive.cache``): one slot holding
# ``{"entry": (validity_token, CorpusGraph | None), "checked_at": monotonic ts}``.
# ``None`` is cached as an entry too — a store with no embedded vectors shouldn't
# re-probe on every search, only when the token moves.
#
# Held by the archive rather than in a module dict keyed on the engine: a corpus
# graph is a partition of *these* threads, and the id of a disposed engine is
# reused, so a keyed-by-engine cache could hand a fresh archive the previous one's
# partition. It also means the graph is released when the archive closes, which
# for a corpus-wide Leiden result is tens of MB.
_SLOT = "embed_graph"
# Engines with a background refresh thread in flight — the guard against spawning
# a second one. It does not cover the build itself: `get(block=True)` builds on the
# calling thread and would otherwise race a background refresh, so the work is
# guarded separately below.
_REFRESHING: set[int] = set()  # archive tokens
_REFRESH_LOCK = threading.Lock()
# One build at a time per engine, whoever asked. The entry points — a background
# refresh, an eval's inline build, a warm pass that found nothing on disk — otherwise
# run the identical build concurrently. The loser waits for the winner's result
# instead of duplicating it, which costs it nothing (it was going to wait out a build
# either way) and halves the CPU and the peak memory of a corpus-wide Leiden
# partition. Within one process only: `rebuild_floor_s` is the cross-process half.
_BUILDING: set[int] = set()  # archive tokens
_BUILD_LOCKS: dict[int, threading.Lock] = {}
_BUILD_GUARD = threading.Lock()
# Probe the validity token (a count scan) and kick a refresh at most this often: the
# graph is a coarse community prior, so a minute of staleness is immaterial, and
# without the gate every search during continuous ingest re-probes and rebuilds.
_REFRESH_COOLDOWN_S = 60.0

_REBUILD_FLOOR_ENV = "THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S"


def rebuild_floor_s() -> float:
    """How recently *any* process must have persisted a graph for this one to skip a
    rebuild, from ``THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S`` (``0`` disables the gate).

    :data:`_REFRESH_COOLDOWN_S` bounds how often one process *probes*; this bounds
    how often the machine pays a *build*, which is the expensive half and the one no
    per-process guard can see. Both are needed because the validity token moves with
    every ingest pass: token staleness alone asks for a rebuild continuously, and
    several processes each holding their own copy of that judgement rebuild the same
    partition over and over — worst at startup, where restarts arrive in bursts and
    every fresh process finds a token that has moved.

    What the coherence re-rank needs is *a* recent community prior, not the current
    one. It is a mid-list orderer over a corpus that grows ~1%/day, and
    :func:`.graph_cache.max_age_s` already puts a week of staleness inside the
    envelope; a quarter hour is two orders of magnitude inside it. A build is never
    gated when there is nothing on disk to serve instead.
    """
    raw = os.environ.get(_REBUILD_FLOOR_ENV)
    if raw is None:
        return 900.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 900.0


def _rebuild_redundant() -> bool:
    """Whether a persisted graph is recent enough that building would repeat work
    another process just did.

    ``False`` whenever there is nothing on disk, so the first build on a fresh
    archive is never gated — the floor suppresses duplicate work, never the only
    copy of it.
    """
    floor = rebuild_floor_s()
    if floor <= 0.0:
        return False
    from . import graph_cache

    age = graph_cache.newest_age_s()
    return age is not None and age < floor


def _refresh_if_stale(arch: "Archive") -> None:
    """Probe the store's validity token and kick a background rebuild when the
    cached graph sits behind it — at most once per :data:`_REFRESH_COOLDOWN_S`, and
    never when :func:`_rebuild_redundant` says the machine already has a recent one.

    Shared by :func:`get` and :func:`warm` so a serving process ages its graph the
    same way however it came by it."""
    now = time.monotonic()
    cache = arch.cache(_SLOT)
    if now - cache.get("checked_at", 0.0) < _REFRESH_COOLDOWN_S:
        return
    cache["checked_at"] = now
    cached = cache.get("entry")
    if cached is None:
        return
    stale = True
    try:
        from .vectors import _validity_token

        with get_session() as s:
            stale = cached[0] != _validity_token(s)
    except Exception:  # noqa: BLE001 — a staleness probe must never break search
        stale = False
    if stale and not _rebuild_redundant():
        _refresh_async(arch.token)


def reset_cache() -> None:
    """Drop this process's graph cache. Memory only — the persisted copy is keyed
    by the store's own token and invalidated at its source
    (:func:`.graph_cache.drop`), so dropping it here would throw away a valid
    cache that costs a rebuild to recreate.

    A no-op when no archive is open — dropping a cache must never be what opens
    one."""
    arch = current_archive_or_none()
    if arch is not None:
        arch.cache(_SLOT).clear()


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
    :data:`COHERENCE_GAMMA`; ``off``/``0`` disable; a float overrides. ``env``
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
    ``score = 1/(60+rank) + gamma * community_mass``."""
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
    ``block=True`` builds inline and requires the current token (eval, tests) — a
    starting server wants :func:`warm` instead."""
    if block:
        return build()
    arch = current_archive()
    cache = arch.cache(_SLOT)
    cached = cache.get("entry")
    if cached is None:
        # Nothing in memory: the first touch of a fresh process. A persisted graph
        # is served without waiting for its token to be checked — serving stale is
        # already this function's contract, and the alternative on offer is not a
        # fresher graph but none at all.
        cached = _disk_entry()
        if cached is None:
            # nothing to serve yet — get the first build going
            _refresh_async(arch.token)
            return None
        cache["entry"] = cached
    _refresh_if_stale(arch)
    return cached[1]


def warm() -> Optional[CorpusGraph]:
    """Make this process's coherence re-rank live, as cheaply as it can be made live
    — the warm pass's graph stage.

    Serves the persisted graph when there is one, stale, exactly as :func:`get`
    serves it and for the same reason: what is on offer is not a fresher graph but a
    re-rank that stands down for the next several seconds, ordering the same query
    differently with nothing in the output to say so. Builds inline only when there
    is nothing on disk to serve at all — a first run, or a build-shape change that
    invalidated every file.

    Distinct from ``get(block=True)`` because the two callers want opposite things
    from the same graph. An eval asks for the graph of the snapshot it is scoring and
    must not inherit whatever a previous process left on disk, so it pays the build.
    A starting server asks only to stop being useless, and paying a corpus-wide
    Leiden partition to answer that is the most expensive way to get an answer it
    already had — the load is tens of MB off disk against seconds of compute.
    """
    arch = current_archive()
    cache = arch.cache(_SLOT)
    if cache.get("entry") is None:
        entry = _disk_entry()
        if entry is None:
            return build()  # nothing to serve: this process pays the first build
        cache["entry"] = entry
    _refresh_if_stale(arch)
    return cache["entry"][1]


def is_refreshing() -> bool:
    """Whether a graph build is in flight in this process — read by the contention
    sample. A build is measured in seconds (Leiden over the whole corpus), so a
    search running beside one is not competing for nothing.

    Reads the build state rather than the background-thread guard: an inline build
    competes with a concurrent search exactly as much as a backgrounded one does, and
    a contention signal that only sees one of them under-reports the case with the
    worst timing — startup."""
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
    from .vectors import _validity_token, ensure_index

    if not ensure_index():  # creates event_vectors when absent, like _knn does
        return None
    with get_session() as s:
        token = _validity_token(s)
    arch = current_archive()
    cache = arch.cache(_SLOT)
    key = arch.token
    cached = cache.get("entry")
    if cached is not None and cached[0] == token:
        return cached[1]

    with _build_lock(key):
        # Re-checked under the lock: whoever we queued behind was building this
        # same token, so their result is ours and the build we were about to do is
        # already done. This is the whole point of the lock — not serializing
        # builds, but making the second one unnecessary.
        cached = cache.get("entry")
        if cached is not None and cached[0] == token:
            return cached[1]
        # Some earlier process already built this exact token and wrote it down.
        # Required to match here (unlike `get`, which serves stale): this is the
        # authoritative path, and callers of `build` are asking for the graph of
        # the store as it stands.
        entry = _disk_entry(knn, min_sim)
        if entry is not None and entry[0] == token:
            cache["entry"] = entry
            return entry[1]
        with _BUILD_GUARD:
            _BUILDING.add(key)
        # Timed around the real build only — every early return above served a
        # cache (memory, or another process's on-disk entry) and rebuilt nothing.
        _t0 = time.perf_counter()
        graph = None
        try:
            graph = _build_graph(cache, token, knn, min_sim)
            return graph
        finally:
            with _BUILD_GUARD:
                _BUILDING.discard(key)
            _record_graph_refresh(_t0, graph)


def _record_graph_refresh(started: float, graph: Optional[CorpusGraph]) -> None:
    """Log the build to the usage ledger — its cost, and the corpus size that
    explains the cost. Fail-soft: the graph is best-effort and its telemetry is
    more so."""
    try:
        from . import _contention
        from .usage import record_refresh

        detail = (
            {"threads": len(graph.thread_ids), "edges": int(graph.edges),
             "communities": len(graph.members)}
            if graph is not None else None
        )
        record_refresh(
            "graph",
            duration_ms=(time.perf_counter() - started) * 1000.0,
            failed=graph is None,
            detail=detail,
            context=_contention.sample(),
        )
    except Exception:  # noqa: BLE001 — advisory
        logger.debug("corpus graph: could not record refresh", exc_info=True)


def _build_lock(key: int) -> threading.Lock:
    """The per-engine build lock, created on first use."""
    with _BUILD_GUARD:
        lock = _BUILD_LOCKS.get(key)
        if lock is None:
            lock = _BUILD_LOCKS[key] = threading.Lock()
        return lock


def _build_graph(cache: dict, token: object, knn: int, min_sim: float) -> Optional[CorpusGraph]:
    """The build proper — always called holding this archive's build lock, with
    ``cache`` (the archive's own graph slot) already checked against ``token``."""
    import networkx as nx

    from .community import detect_communities
    from .vectors import _build_matrix_entry

    # The authoritative build reads the live matrix directly (the pure builder),
    # not the search path's serve-stale cache — the graph must reflect the store
    # it just tokened, and this call is already gated by the graph token cache above.
    _tok, ids, _ct_arr, mat, _doc_inv, _doc_rep, scope_rows, _ts = _build_matrix_entry(_CTS)
    if len(ids) == 0:
        cache["entry"] = (token, None)  # no vectors: don't re-probe until the store moves
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
        cache["entry"] = (token, None)
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
    cache["entry"] = (token, graph)
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
