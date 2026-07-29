"""Candidate-pool cache — opt-in, fail-soft, zero-cost when unused.

A search splits cleanly in two. The **pool** half (the FTS5 scan, the query
embed, the vector matvec, RRF fusion, dedup) depends on the query, the structural
filters, and the two pool-shaping knobs — ``pool_floor`` and ``rrf_k``. The
**ranking** half (:func:`.rank.rank_search_results` and everything after it)
depends on the weights. Tuning weights therefore re-pays the pool half on every
configuration for a pool it already computed: measured over the findability gold
file, that is ~90% of the wall-clock of a scoring run.

This module is the seam that stops paying it. The contract mirrors
:mod:`._probe`: a caller installs a cache for the duration of a block
(``with install(cache):``) and :func:`thread_archive._retrieval.retrieve_pool`
consults whatever is current. Nothing installed → :func:`current` is ``None`` and
the check is one ``is None``, so the request path is untouched. Production never
installs one; the eval bench does.

**The key is the whole correctness story.** :func:`key_for` names every input the
pool depends on, ``rrf_k`` and the effective pool depth included. Omitting one
does not produce a slow cache, it produces a *silent* one: sweep a knob the key
ignores and every configuration reads back the first configuration's pool, so the
knob measures as having no effect — a false negative indistinguishable from a
real one. Anything added to :class:`~.params.SearchParams` that reaches the arms
or the fusion belongs in the key.

Two things the key deliberately cannot see, because they are ambient rather than
arguments: **which corpus** is open, and **when**. The caller supplies
``namespace`` for the first (a snapshot-bound run passes the snapshot's
``snapshot_id``, so a re-snapshot lands in a different namespace rather than
serving stale hits).
The second is unguarded — a cached pool is a point-in-time read of a mutable
index, so a cache pointed at the *live* archive goes stale as events arrive.
Frozen snapshots are what this is for.
"""

from __future__ import annotations

import contextvars
import logging
import pickle
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from ._types import EventHit

logger = logging.getLogger(__name__)

_CURRENT: contextvars.ContextVar[Optional["PoolCache"]] = contextvars.ContextVar(
    "thread_archive_pool_cache", default=None
)

#: Bumped when the cached pool's shape changes (a new field the ranker reads, a
#: change to what ``retrieve_pool`` returns). Part of every key, so a stale
#: on-disk cache from an older build misses instead of feeding the ranker a pool
#: it can no longer score correctly.
FORMAT_VERSION = 4


def key_for(
    query: str,
    *,
    over: int,
    rrf_k: int,
    structural: bool,
    thread_id: object = None,
    thread_ids: Optional[list[str]] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    agents: str = "exclude",
    startswith: Optional[str] = None,
    oldest_first: bool = False,
    or_fallback: bool = True,
    path: Optional[str] = None,
    match_mode: str = "token",
) -> tuple:
    """The cache key for one pool: every input that changes what the arms return
    or how they fuse.

    ``over`` is the already-resolved pool depth (it folds ``limit`` and
    ``pool_floor``, the two knobs that set it) and ``rrf_k`` the fusion constant —
    the two ``SearchParams`` fields that reach this half of the pipeline.
    ``match_mode`` selects the lexical arm's predicate outright (indexed token
    MATCH vs uncapped infix scan), so two modes over one query are two different
    pools. The rest are the query and its structural scope. Ordering-insensitive
    for the list arguments, so ``['user','text']`` and ``['text','user']`` share
    a pool.
    """
    def norm(v: Optional[list[str]]) -> Optional[tuple[str, ...]]:
        return tuple(sorted(v)) if v else None

    return (
        FORMAT_VERSION, query, over, rrf_k, structural,
        thread_id, norm(thread_ids), norm(content_types), norm(exclude_content_types),
        since, until, tool_name, norm(source), norm(types),
        agents, startswith, oldest_first, or_fallback, path, match_mode,
    )


class PoolCache:
    """A keyed store of fused candidate pools, optionally backed by a file.

    Hits are handed out as fresh per-hit dicts. The pipeline downstream *mutates*
    the hits it ranks — ``thread_title`` and ``context`` get written onto them —
    so a cache that returned its own dicts would let one configuration's
    enrichment leak into the next one's scoring. The stored copy is never handed
    out.

    ``namespace`` scopes the whole cache to one corpus (see the module docstring);
    it is written into the file and a load from a different namespace is discarded
    rather than served.
    """

    def __init__(self, namespace: str = "", path: Optional[Path] = None) -> None:
        self.namespace = namespace
        self.path = Path(path) if path else None
        self._pools: dict[tuple, list[EventHit]] = {}
        self.hits = 0
        self.misses = 0
        if self.path is not None:
            self._load()

    def get(self, key: tuple) -> Optional[list[EventHit]]:
        """The cached pool for ``key`` as fresh dicts, or ``None``."""
        pool = self._pools.get(key)
        if pool is None:
            self.misses += 1
            return None
        self.hits += 1
        return [h.copy() for h in pool]

    def put(self, key: tuple, pool: list[EventHit]) -> None:
        """Store a copy of ``pool``, insulated from the caller's later mutations."""
        self._pools[key] = [h.copy() for h in pool]

    def __len__(self) -> int:
        return len(self._pools)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def _load(self) -> None:
        """Read a previously saved cache; any failure leaves an empty one.

        Fail-soft by construction: the cache is an optimization, so a truncated
        file, an unreadable one, or one written under a different namespace or
        format version costs a re-fetch, never a wrong answer or a crash."""
        assert self.path is not None
        if not self.path.is_file():
            return
        try:
            blob: Any = pickle.loads(self.path.read_bytes())
            if (blob.get("namespace") == self.namespace
                    and blob.get("format") == FORMAT_VERSION):
                self._pools = blob["pools"]
        except Exception:  # noqa: BLE001 — a bad cache file must never break a run
            logger.debug("pool cache: discarding unreadable %s", self.path, exc_info=True)

    def save(self) -> None:
        """Persist to ``path`` (no-op without one), so the *next process* starts
        warm — the loop this exists for is re-running a scorer with new weights,
        which is a new process every time.

        Written to a sibling temp file and renamed, so an interrupted save leaves
        the previous cache intact rather than a truncated one."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        blob = {"format": FORMAT_VERSION, "namespace": self.namespace, "pools": self._pools}
        try:
            tmp.write_bytes(pickle.dumps(blob, protocol=pickle.HIGHEST_PROTOCOL))
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001 — failing to persist is not failing the run
            logger.debug("pool cache: could not save %s", self.path, exc_info=True)
            tmp.unlink(missing_ok=True)


def current() -> Optional[PoolCache]:
    """The cache the current context installed, or ``None`` — the guard
    ``retrieve_pool`` checks so an uncached search pays nothing."""
    return _CURRENT.get()


@contextmanager
def install(cache: Optional[PoolCache] = None) -> Iterator[PoolCache]:
    """Install ``cache`` (default: a fresh in-memory one) for the block and yield
    it. Restores the prior slot on exit, so caches nest without leaking.

    Passing ``None`` explicitly is not a way to *disable* an outer cache — it
    installs a new empty one. Use :func:`suspend` for that."""
    cache = cache if cache is not None else PoolCache()
    token = _CURRENT.set(cache)
    try:
        yield cache
    finally:
        _CURRENT.reset(token)


@contextmanager
def suspend() -> Iterator[None]:
    """Run the block with no cache installed — the escape hatch for a search that
    must hit the real index while a cache is in scope (re-measuring a pool,
    checking a cached result against the live one)."""
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)
