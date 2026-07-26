"""Search + read over the SQLite store.

``search`` runs the production pipeline: federate two arms — FTS5 **lexical** + an
optional in-process **vector** (semantic) search — fuse them by reciprocal-rank
fusion (``_rrf`` normalized to [0,1]), dedup, score with the weighted lexical
**ranker** (density / phrase / recency / content-type / fusion — :mod:`.rank`),
then optionally re-order the head with an in-process **cross-encoder** (:mod:`.rerank`)
— gated twice: to conceptual multi-term query shapes (``should_rerank``), and away
again when the ranked head is already a strong literal match (``head_is_strong``) —
the re-rank pays its seconds only on the vocab-mismatch queries it was built for.
With no ``[embeddings]`` extra the vector and
cross-encoder arms sit out and search is lexical-only (still through the ranker).
``read_thread`` reconstructs a conversation; ``rebuild_fts`` is the FTS half of reindex.
:mod:`.code` is the other retrieval axis — the files a conversation touched and the
commits it produced, indexed structurally rather than as text.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from datetime import datetime
from time import perf_counter
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Event, Thread, use_session
from . import _probe, fts, pool_cache
from . import embed_graph as _embed_graph
from . import rank as _rank
from ._classify import resolve_relative_date
from ._context import extract_context_lines, get_context_events, parse_context_events_spec
from ._types import EventHit, Pool, Results
from .browse import browse_threads
from .code import (
    blame_commit,
    blame_path,
    code_index_status,
    rebuild_code_index,
    refresh_code_index,
    thread_files,
)
from .format import COUNT_FETCH_CAP, format_results
from .fts import ensure_fts, fts_status, index_events, index_thread_meta, rebuild_fts, search_events
from .params import DEFAULT as _DEFAULT_PARAMS
from .params import SearchParams
from .read import read_thread, read_thread_structured, resolve_thread_ref

logger = logging.getLogger(__name__)

#: The content scope a search runs in when its caller names none: everything the
#: index holds except derived thread summaries, which are the librarian's text
#: rather than the record and so stay opt-in.
#:
#: Defined here rather than at the agent surface because two callers must agree on
#: it: the surface, and :func:`warm_models` — the vector matrix caches per
#: content-type scope, so a warm pass primed against a different scope leaves the
#: first real query to build a matrix inside the request.
DEFAULT_CONTENT_TYPES: Optional[list[str]] = None
DEFAULT_EXCLUDE_CONTENT_TYPES = ("summary",)


def _enrich_thread_titles(hits: list[EventHit], *, session: Optional[Session] = None) -> None:
    """Fill ``thread_title`` on hits from the threads table (one query)."""
    if not hits:
        return
    ids = {h["thread_id"] for h in hits}
    with use_session(session) as s:
        rows = s.execute(select(Thread.id, Thread.title, Thread.name).where(Thread.id.in_(ids))).all()
    titles = {tid: (title or name) for tid, title, name in rows}
    for h in hits:
        h["thread_title"] = titles.get(h["thread_id"])


def _enrich_thread_rows(hits: list[EventHit], *, session: Optional[Session] = None) -> None:
    """Fill the thread-list columns — provider and event count — on hits the
    thread-granular shapes render as threads rather than as messages (one query).
    Mirrors what :func:`.browse.browse_threads` puts on a browse row, so the same
    row vocabulary reads the same whether the list came from a query or not."""
    if not hits:
        return
    ids = {h["thread_id"] for h in hits}
    n_events = (
        select(func.count()).where(Event.thread_id == Thread.id).scalar_subquery()
    )
    with use_session(session) as s:
        rows = s.execute(
            select(Thread.id, Thread.source, n_events.label("n_events"))
            .where(Thread.id.in_(ids))
        ).all()
    meta = {r.id: r for r in rows}
    for h in hits:
        row = meta.get(h["thread_id"])
        if row is not None:
            h["thread_source"] = row.source
            h["n_events"] = row.n_events


def _rrf_merge(result_lists: list[list[EventHit]], limit: int, k: int = 60) -> list[EventHit]:
    """Reciprocal-rank fusion of several ranked hit lists, keyed by
    (event_id, content_type). RRF score = Σ 1/(k + rank), **normalized to [0,1]**
    (÷ peak) so the ranker's ``fusion_weight`` is calibrated against it. The arm
    provenance (``_semantic`` cosine) is carried onto the fused hit even when an
    earlier arm's copy is the one kept."""
    scores: dict = {}
    chosen: dict = {}
    for lst in result_lists:
        for rank, h in enumerate(lst):
            key = (h["event_id"], h.get("content_type"))
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in chosen:
                chosen[key] = h
            elif "_semantic" in h and "_semantic" not in chosen[key]:
                chosen[key]["_semantic"] = h["_semantic"]
    peak = max(scores.values()) if scores else 0.0
    ranked = sorted(chosen, key=lambda key: (-scores[key], chosen[key]["event_id"]))
    out = []
    for key in ranked[:limit]:
        chosen[key]["_rrf"] = round(scores[key] / peak, 6) if peak > 0 else 0.0
        out.append(chosen[key])
    return out


def _semantic_hits(query, *, thread_id, content_types, exclude_content_types, since, until, over,
                   source, thread_ids=None, agents="exclude", path=None, embedder=None):
    """The vector arm — None when the extra is absent, nothing's indexed, or embed fails."""
    try:
        from . import vectors

        if not vectors.is_available():
            return None
        return vectors.search(
            query, thread_id=thread_id, content_types=content_types,
            exclude_content_types=exclude_content_types, limit=over, since=since, until=until,
            source=source, thread_ids=thread_ids, agents=agents, path=path, embedder=embedder,
        )
    except Exception:  # noqa: BLE001 — vector arm must never break lexical search
        logger.exception("semantic arm failed; search continues lexical-only")
        return None


def _apply_coherence(ranked: list[EventHit], gamma: float | None = None) -> list[EventHit]:
    """Community-coherence re-rank at thread granularity (fail-soft).

    Reorders the ranked hit list so threads follow :func:`embed_graph.coherence_order`
    — the eval-proven boost for threads whose corpus-graph community carries more
    of the pool's top mass. Hits within a thread keep their relative order. A
    no-op when coherence is off, the graph isn't built yet (the background
    refresh will have it soon), or anything fails. ``gamma`` overrides the env
    knob (tests inject)."""
    if gamma is None:
        gamma = _embed_graph.coherence_gamma()
    if gamma <= 0.0 or len(ranked) < 3:
        return ranked
    try:
        graph = _embed_graph.get()
        if graph is None:
            return ranked
        pool: list[str] = []
        seen: set[str] = set()
        for r in ranked:
            t = r.get("thread_id")
            if t and t not in seen:
                seen.add(t)
                pool.append(t)
        order = _embed_graph.coherence_order(pool, graph.community, gamma)
        pos = {t: i for i, t in enumerate(order)}
        return sorted(ranked, key=lambda r: pos.get(r.get("thread_id") or "", len(order)))
    except Exception:  # noqa: BLE001 — a ranking refinement must never break search
        logger.exception("coherence re-rank failed; search continues without it")
        return ranked


# A throwaway conceptual query for the warm pass (run with rerank=True so the
# cross-encoder head is exercised regardless of the gates).
_WARM_QUERY = "warm up the retrieval vector index and reranker"


def warm_models(embedder=None, reranker=None) -> None:
    """Prime the whole retrieval pipeline on the caller's thread so the FIRST real search
    doesn't pay startup costs *inside* the request. Those costs — cold-loading the embedding
    + cross-encoder models (tens of seconds), loading the vector matrix off disk, and the
    first cross-encoder inference — otherwise land on the first query and can exceed an MCP
    client's request timeout (see :mod:`thread_archive._mcp.server`, which calls this on a
    background thread at startup).

    Three steps, ordered by what a request actually waits on: load the models
    explicitly (works even with an empty store), run one throwaway conceptual search to
    fill the process-global caches the first real query reuses (the vector matrix, the
    reranker's warmed inference path), and only then build the corpus graph. The graph is
    last because it is the one stage no search blocks on — the coherence re-rank serves
    whatever is cached and returns the ranking unchanged when nothing is — while it is
    also the longest (tens of seconds on a real corpus). Building it before the priming
    search would leave the process paying full cold-search latency for that whole window,
    which is precisely the cost this function exists to move off the request path.
    ``embedder`` and ``reranker`` are the models to prime (default: the process ones).
    Fail-soft throughout: a missing ``[embeddings]`` extra, a load failure, or an
    unavailable store just leaves search to cold-load lazily, exactly as before.

    The pass times itself into the usage ledger. This is the startup cost the whole
    function exists to move off the request path, and moving a cost is not the same
    as removing it: until it is recorded, "how long after a restart is this server
    actually useful" has no answer, and a model load that slowly regresses past an
    MCP client's timeout looks identical to one that doesn't."""
    started = perf_counter()
    stage_ms: dict[str, float] = {}
    failed: list[str] = []

    if embedder is None:
        from . import embed as _embed

        embedder = _embed.default()
    if reranker is None:
        from . import rerank as _rerank

        reranker = _rerank.default()

    for name, stage in (("embed", embedder), ("rerank", reranker)):
        _t = perf_counter()
        try:
            stage.warm()
        except Exception:  # noqa: BLE001 — warming is best-effort; never raise into a caller
            failed.append(name)
            logger.debug("warm_models: a model stage failed to preload", exc_info=True)
        stage_ms[name + "_ms"] = (perf_counter() - _t) * 1000.0

    # Run one throwaway search end to end: it loads the vector matrix and runs a first
    # cross-encoder inference, both of which cache process-globally for the real queries.
    # Scoped to :data:`DEFAULT_CONTENT_TYPES` so the matrix this primes is keyed the
    # same as the real queries reuse (the matrix cache is keyed by content-type scope;
    # a mismatched scope would prime a matrix the real query never touches).
    _t = perf_counter()
    try:
        from .. import _api as api

        # rerank=True: the point is priming the cross-encoder's inference path, so
        # force it past the gates (a strong-headed warm hit would otherwise skip it).
        api.search(_WARM_QUERY, limit=1, content_types=DEFAULT_CONTENT_TYPES,
                   exclude_content_types=list(DEFAULT_EXCLUDE_CONTENT_TYPES), rerank=True)
    except Exception:  # noqa: BLE001 — a store that isn't ready just warms the models, not the caches
        failed.append("search")
        logger.debug("warm_models: dummy warm search skipped", exc_info=True)
    stage_ms["search_ms"] = (perf_counter() - _t) * 1000.0

    # Build the corpus graph inline while we're already off the request path — the
    # coherence re-rank serves from this cache and never builds during a search (a
    # stale graph refreshes in the background; the FIRST build is the warm pass's
    # job). Last of the stages: a search runs correctly without it, so every second
    # spent here before the steps above would be a second of cold search latency.
    if _embed_graph.coherence_gamma() > 0.0:
        _t = perf_counter()
        try:
            _embed_graph.get(block=True)
        except Exception:  # noqa: BLE001 — warming is best-effort
            failed.append("graph")
            logger.debug("warm_models: corpus graph build skipped", exc_info=True)
        stage_ms["graph_ms"] = (perf_counter() - _t) * 1000.0

    try:
        from . import usage as _usage

        _usage.record_warm(
            duration_ms=(perf_counter() - started) * 1000.0,
            stages=stage_ms,
            failed=failed,
        )
    except Exception:  # noqa: BLE001 — telemetry is advisory; warming stays fail-soft
        logger.debug("warm_models: could not record the warm pass", exc_info=True)


def retrieve_pool(
    query: str,
    *,
    over: int,
    structural: bool,
    params: Optional[SearchParams] = None,
    thread_id: Optional[str] = None,
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
    path: Optional[str] = None,
    oldest_first: bool = False,
    or_fallback: bool = True,
    match_mode: str = "token",
    embedder=None,
    session: Optional[Session] = None,
) -> list[EventHit]:
    """The federation half of a search: run the arms, fuse them, dedup — the
    candidate pool the ranker then orders.

    Split out from :func:`search` because the two halves have different
    dependencies. This one reads the query, the structural scope, and exactly two
    ``SearchParams`` fields (``pool_floor``, folded into ``over`` by the caller,
    and ``rrf_k``); every ranking weight is invisible to it. That makes the pool
    reusable across configurations, which is what :mod:`.pool_cache` exploits —
    and this is where an installed cache is consulted. Nothing installed (always,
    in production) and it is a plain call.

    ``over`` is the pool depth, resolved by the caller. ``structural`` marks the
    shapes with no lexical MATCH to embed against (prefix scan, chronological
    sort, count), which sit the vector arm out. ``match_mode='substring'`` runs
    the lexical arm as an uncapped infix scan (see :func:`.fts.search_events`);
    the vector arm is unaffected, since a substring ask is about the literal text
    and semantic neighbours are still the right second opinion on it.
    """
    p = params or _DEFAULT_PARAMS
    probe = _probe.current()
    cache = pool_cache.current()

    key = None
    if cache is not None:
        key = pool_cache.key_for(
            query, over=over, rrf_k=p.rrf_k, structural=structural,
            thread_id=thread_id, thread_ids=thread_ids, content_types=content_types,
            exclude_content_types=exclude_content_types, since=since, until=until,
            tool_name=tool_name, source=source, types=types, agents=agents,
            startswith=startswith, oldest_first=oldest_first, or_fallback=or_fallback,
            path=path, match_mode=match_mode,
        )
        cached = cache.get(key)
        if cached is not None:
            if probe is not None:
                probe.pool_size = len(cached)
            # A cached pool kept its rows, not its reach; assume it filled, which
            # is the conservative read (it makes a caller verify rather than trust
            # a completeness claim the cache can't back up).
            return Pool(cached, raw=max(len(cached), over))

    # A tool_name scope also sits the vector arm out: tool docs aren't embedded
    # (only user/text/title/summary are), so every semantic hit in a tool-scoped
    # search would be a hit the filter should have excluded. A types scope sits
    # it out too: vectors carry no thread-type filter, so its hits could leak
    # threads the filter excludes.
    #
    # The two arms share only the query and the scope — neither reads the other's
    # output — so they run **concurrently** and the pool waits on the slower of the
    # two rather than on their sum. The overlap is real rather than bookkeeping:
    # both arms spend nearly all their time inside code that releases the GIL
    # (SQLite's C-level scan, the matvec over the mmapped pack, the embedder's
    # inference), so two Python threads genuinely run at once here.
    #
    # The vector arm is the one that moves off the calling thread. It opens its own
    # sessions, where the lexical arm may have been handed the *caller's* — and a
    # Session belongs to the thread that owns it — so leaving the lexical arm in
    # place is what keeps a supplied session on its owner's thread.
    #
    # A copied context, not a bare thread: the engine override (``use_engine``) and
    # the timing probe both live in context variables, and a thread started without
    # them would query the process-global store and record its stages nowhere. The
    # two arms write disjoint probe stages, so sharing one probe across the pair
    # needs no lock.
    semantic_arm: dict = {}

    def _vector_arm() -> None:
        _t = perf_counter()
        try:
            semantic_arm["hits"] = _semantic_hits(
                query, thread_id=thread_id, content_types=content_types,
                exclude_content_types=exclude_content_types, since=since, until=until,
                over=over, source=source, thread_ids=thread_ids, agents=agents,
                path=path, embedder=embedder,
            )
        finally:
            semantic_arm["ms"] = (perf_counter() - _t) * 1000.0

    worker = None
    if not (structural or tool_name or types):
        worker = threading.Thread(
            target=contextvars.copy_context().run, args=(_vector_arm,),
            name="retrieval-vector-arm", daemon=True,
        )
        worker.start()

    _t0 = perf_counter()
    try:
        lexical = search_events(
            query, thread_id=thread_id, thread_ids=thread_ids, content_types=content_types,
            exclude_content_types=exclude_content_types, limit=over,
            since=since, until=until, tool_name=tool_name, source=source,
            types=types, agents=agents, path=path,
            startswith=startswith, oldest_first=oldest_first,
            or_fallback=or_fallback, match_mode=match_mode, session=session,
        )
    finally:
        # Joined in a finally so a raising lexical arm still reaps its peer: an
        # abandoned worker would go on holding a session and a matrix reference
        # past the request that started it.
        if probe is not None:
            probe.fts_ms += (perf_counter() - _t0) * 1000.0
        if worker is not None:
            worker.join()

    # Stamp each lexical hit with where the arm itself placed it — bm25 standing
    # for a MATCH pass. FTS5 orders by bm25 but does not surface the score, and the
    # ranker's own lexical signal (density) is IDF-blind: it counts matched terms
    # per ``density_norm_chars``, weighing a corpus-wide common term exactly like
    # the rare one that actually discriminates, then divides by length. So a short
    # doc carrying a few common query words outscores the long doc carrying the
    # discriminating ones. The reciprocal rank (peak-normalized to 1.0 at the head,
    # the same shape ``_rrf_merge`` uses) puts the arm's own verdict in a form the
    # scorer can weigh; ``bm25_weight`` is what admits it (see params.py).
    for _i, _h in enumerate(lexical):
        _h["_lex"] = round((p.rrf_k + 1) / (p.rrf_k + 1 + _i), 6)

    # Beside that positional proxy, the arm's own bm25 *magnitude* (``_bm25``,
    # stamped raw by the MATCH passes), peak-normalized to [0,1] like ``_rrf`` and
    # ``_lex`` so ``bm25_score_weight`` is calibrated against a fixed scale rather
    # than a per-query one (bm25's raw range moves with term count and IDF).
    # The two lexical signals are not redundant: the proxy is a near-flat gradient
    # by construction — at rrf_k=60 the whole 200-deep pool spans 1.00 down to 0.23
    # — so it can only nudge, where the score separates a doc carrying the rare
    # discriminating term from one carrying three common ones. Hits with no score
    # (the substring-scan pass, semantic-only hits) read 0.0, exactly as they
    # already do for ``_lex``.
    # Normalized unconditionally, so a raw per-query magnitude can never reach the
    # ranker: with no positive peak to divide by there is no scale to weigh against,
    # and the signal reads 0.0 rather than whatever the arm happened to return.
    _peak = max((_h.get("_bm25", 0.0) or 0.0 for _h in lexical), default=0.0)
    for _h in lexical:
        if "_bm25" in _h:
            _h["_bm25"] = round(max(_h["_bm25"], 0.0) / _peak, 6) if _peak > 0 else 0.0

    # The vector arm's result, joined above. Absent when the arm sat out, and also
    # when it died in a way ``_semantic_hits`` could not swallow — both read as
    # None, which is exactly the lexical-only fallback the fusion below expects.
    semantic = semantic_arm.get("hits")
    if probe is not None and "ms" in semantic_arm:
        probe.semantic_ms += semantic_arm["ms"]

    # Fuse the arms into a wide pool (keeps _rrf agreement + _semantic provenance),
    # then dedup byte-identical hits before ranking.
    # ``raw`` before fusion/dedup shrink it — the pool's own reach, which is what
    # tells a caller whether the arms ran out of matches or ran out of room.
    raw = max(len(lexical), len(semantic or ()))
    fused = _rrf_merge([lexical, semantic], over, k=p.rrf_k) if semantic else lexical
    fused = Pool(_rank.dedup_results(fused), raw=raw)
    if probe is not None:
        probe.pool_size = len(fused)
    if cache is not None and key is not None:
        cache.put(key, fused)
    return fused


def _nested_page(clustered: list[EventHit], offset: int, limit: int) -> list[EventHit]:
    """One page of a nested result, counted in threads.

    ``group='nested'`` returns every hit clustered under its thread, so a row
    slice would split one thread's hits across a page boundary — the reader would
    see a thread's 2nd and 3rd matches with no idea the 1st was on the previous
    page. Windows whole clusters instead, keeping their order."""
    order: list[str] = []
    for r in clustered:
        tid = r["thread_id"]
        if tid not in order:
            order.append(tid)
    keep = set(order[offset:offset + limit])
    return [r for r in clustered if r["thread_id"] in keep]


def _extend_with_unranked_threads(
    ranked: list[EventHit],
    query: str,
    *,
    match_mode: str,
    startswith: Optional[str],
    oldest_first: bool,
    scope: dict,
    session: Optional[Session] = None,
) -> tuple[list[EventHit], bool]:
    """Reconcile a ranked page against the real match set, so a thread list
    enumerates exactly the threads that match. Returns ``(rows, capped)``.

    The match set is authoritative here and the pool supplies only *order*. Both
    directions of the reconciliation matter:

    - Threads past the pool boundary are invisible to the ranker — not ranked
      low, **absent** — and a list that quietly ends there is indistinguishable
      from one that ended because the matches did. They are appended behind the
      ranked rows, ordered by their newest match, which is the only ordering that
      exists for rows no ranking pass ever scored.
    - Threads *in* the pool but not in the set are dropped. The pool is federated:
      the vector arm contributes semantic neighbours that need not contain the
      query at all. Those are a relevance aid, not set membership — and since the
      pool deepens with the requested page, letting them count would make the
      total grow as a caller pages through it, which is worse than no total.

    ``capped`` means the set scan itself stopped early
    (:data:`.fts.SET_SCAN_CAP`), so the enumeration is a floor and the dropping
    above is suspended — a thread the truncated scan simply never reached must
    not be deleted from a page it legitimately ranked onto.

    Appended rows carry the same columns as a browse row, and their
    ``_thread_more`` counts raw matching events rather than the post-collapse
    hits the ranked rows report — the pool is what collapses same-anchor twins,
    and these never entered it.
    """
    rows, capped = fts.matched_threads(
        query, match_mode=match_mode, startswith=startswith, session=session, **scope
    )
    if not rows:
        return ([], capped) if not capped else (ranked, capped)
    in_set = {r["thread_id"] for r in rows}
    if not capped:
        ranked = [r for r in ranked if r["thread_id"] in in_set]
    present = {r["thread_id"] for r in ranked}
    tail: list[EventHit] = []
    for row in rows:
        if row["thread_id"] in present:
            continue
        hit: EventHit = {
            "event_id": row["event_id"] or 0,
            "thread_id": row["thread_id"],
            "thread_title": None,
            "event_type": "thread",
            "content_type": None,
            "snippet": "",
            "full_content": "",
            "occurred_at": _parse_stored_dt(row["last_match"]),
        }
        if row["n_hits"] > 1:
            hit["_thread_more"] = row["n_hits"] - 1
        tail.append(hit)
    # matched_threads returns newest-match-first; a chronological ask wants the
    # earliest of the unranked threads to lead.
    if oldest_first:
        tail.reverse()
    return list(ranked) + tail, capped


def _parse_stored_dt(value) -> Optional[datetime]:
    """A stored ``occurred_at`` string as the naive datetime the hit shape carries
    (see :func:`.fts.build_event_hit`, which does the same for a pool row)."""
    if isinstance(value, datetime) or value is None:
        return value
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def _do_rerank(query: str, terms: list[str], force: Optional[bool], reranker,
               auto_enabled: bool) -> bool:
    """The query-shape half of the re-rank gate. ``force`` (the ``rerank=`` arg)
    overrides everything; otherwise the auto path runs only when ``auto_enabled``
    (``params.rerank_auto``) — off by default, the cross-encoder being the
    pipeline's dominant latency for ~no gold-file gain — and the query has the
    conceptual multi-term shape *and* a reranker is available. The result-side
    half — standing down on a strong lexical head — runs after ranking in
    ``search``."""
    if force is not None:
        return force and reranker.is_available()
    return auto_enabled and _rank.should_rerank(query, terms) and reranker.is_available()


def search(
    query: str,
    *,
    limit: int = 20,
    thread_id: Optional[int | str] = None,
    topic_id: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    agents: Optional[str] = None,
    startswith: Optional[str] = None,
    path: Optional[str] = None,
    path_ops: Optional[list[str]] = None,
    thread_ids: Optional[list[str]] = None,
    sort: Optional[str] = None,
    group: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
    match: str = "token",
    page: int = 1,
    params: Optional[SearchParams] = None,
    embedder=None,
    reranker=None,
    session: Optional[Session] = None,
) -> list[EventHit]:
    """Search over conversation events through the production pipeline: lexical FTS5
    + optional semantic vectors → RRF fusion → dedup → weighted lexical rank →
    optional cross-encoder head re-rank. Returns event-hit dicts with the thread
    title enriched. ``since``/``until`` accept ISO timestamps or a relative ``<N>d``
    window; ``source`` restricts to threads of the named provider(s); ``topic_id``
    restricts to a topic's member conversations (threads cited under the topic or
    linked to it); ``embedder`` and ``reranker`` are the models the semantic arm and
    the head re-rank run on (default: the process ones); ``rerank`` forces the
    cross-encoder stage on/off (else auto-gated:
    conceptual multi-term shape AND a ranked head that isn't already a strong
    literal match). ``params`` is the retrieval configuration
    (:class:`.params.SearchParams` — every ranking weight and pool size;
    default: the shipped values), the seam the search lab scores candidate
    configurations through.

    An **empty query** is a browse (see :mod:`.browse`): one row per thread by
    last activity, honoring ``since``/``until``/``source``/``types``/``topic_id``
    and ``sort='oldest'``; content-type and context options don't apply.
    ``types`` restricts to the named ``thread_type`` values (a browse without it
    hides topics and system threads); with a query it scopes the keyword search
    the same way — and sits the semantic arm out, since vectors carry no
    thread-type filter.

    ``path`` restricts to the conversations that **touched a file** — the code axis
    (:mod:`.code`), in the pattern shapes :func:`.code.path_predicate` reads (bare
    name, partial path, absolute path or directory subtree, glob), narrowed by
    ``path_ops`` to particular verbs. It composes both ways the tool already works:
    with a query it scopes the search ("what did we say about retries, among the
    sessions that edited rank.py"), and with an **empty query** it is the code-axis
    browse — the conversations that worked on that file, ordered changes-first, each
    row carrying its op tally and opening at the touch rather than at the thread's
    tail (see :func:`.browse.browse_threads`).

    ``thread_ids`` is a pre-resolved id-set scope, for a caller that worked out the
    conversations itself — the MCP layer's ``commit`` scope resolves there rather
    than here, because a sha that matches nothing scopes to nothing and the caller
    has to be told *that* instead of being handed an empty result. It intersects
    with ``topic_id`` rather than replacing it.

    ``startswith`` does a structural prefix scan (query text unused). ``sort='oldest'``
    returns the earliest matches chronologically, bypassing the ranker — the lexical
    scan itself runs oldest-first, so the pool holds the true first mentions rather
    than a chronological sort of bm25's favourites. ``output='count'``
    returns the whole match pool unranked (the renderer tallies per-thread). With a
    structural shape (browse / startswith / oldest / count) the semantic arm and the
    cross-encoder sit out. ``context_lines`` (default 2; 0 = the raw FTS snippet)
    attaches a numbered window around each hit's match; ``context_events`` (``N`` /
    ``b:a`` / ``b:a:types``) attaches the neighbouring events. Both enrich the
    ``agents`` controls agent-run threads (``thread_type='system'`` — subagent /
    machinery sessions): 'exclude' (default) keeps them out of every result
    shape, 'include' searches/lists them alongside conversations, 'only'
    restricts to nothing else. Deliberate scopes stand it down: an explicit
    ``thread_id``/``topic_id`` bypasses it (like the blacklist), and an explicit
    ``types`` list — the raw thread-type scope — wins over it entirely.

    ``group`` picks how hits relate to threads. The default ranked shape,
    ``group='thread'``, returns **one row per thread** (:func:`rank.group_by_thread`):
    a thread's best hit represents it, with further hits folded into its
    ``_thread_more`` count and cross-thread duplicate content (forks, fleets of
    spawned agents carrying one prompt) folded into ``_dup_thread_ids`` — the
    fold annotates rather than discards. ``group='none'`` returns every ranked
    hit as its own row; ``group='dup'`` folds only the cross-thread duplicates
    (:func:`rank.fold_duplicate_threads`), keeping each surviving thread's own
    hits — the reader's shape, where a result list lays a thread's matches out
    rather than collapsing them to a count.

    Two further modes turn a keyword search into a thread-granular *list*, the
    shape an empty query already returns, and are flagged ``_group`` for the
    renderer. ``group='browse'`` is the list of matched **threads** — one row
    each, thread metadata instead of a snippet, ``limit`` counting threads.
    ``group='nested'`` is the same list with the **messages** kept, clustered
    under their thread (:func:`rank.cluster_by_thread`): ``limit`` counts
    threads there too, each capped at :data:`rank.NESTED_HITS_PER_THREAD` hits
    with the remainder folded into the cluster's ``_thread_more``. Both
    enumerate every matched thread — no cross-thread duplicate fold, which would
    drop a thread from a list that exists to enumerate them — and both apply to
    the structural shapes and under a ``thread_id`` scope, since asking for them
    is explicit. ``output='count'`` still wins over either (it tallies the whole
    unranked pool per thread already).

    Otherwise a ``thread_id`` scope, the structural shapes, and the
    ``count``/``linkable`` outputs are never grouped. Hits sharing one
    ``(thread_id, event_id)`` anchor (a thread-meta title/summary doc and the
    first event it anchors to) collapse to the better-placed row in every
    row-shaped output, grouped or not.

    ``match`` selects what "matches" means. ``'token'`` (default) is the indexed
    FTS5 pipeline described above. ``'substring'`` replaces the lexical arm's
    predicate with an uncapped infix scan, so ``p4`` finds ``mp4`` and ``p400`` —
    matches no index can see. It costs a full-table scan and is deliberately
    opt-in; see :func:`.fts.search_events`.

    ``page`` (1-based) walks the result set. Every page is a slice of ONE
    ordering: nothing that shapes the order — the pool depth, the cross-encoder's
    head — is allowed to depend on which page was asked for, because the
    coherence re-rank scores a thread's community against the pool's mass, so a
    pool that grew per page would hand each page a differently-ordered list and a
    walk would repeat rows while skipping others.

    The returned :class:`._types.Results` carries the match set's size beside the
    page. For the thread-granular list shapes that size is **exact** and every
    matched thread is reachable by paging (``exhaustive``): membership comes from
    :func:`.fts.matched_threads` rather than from the pool, so a thread ranked
    past the pool boundary is enumerated rather than silently dropped. Ranked
    order still leads — the threads the pool reached, in the order it ranked
    them — and the remainder follows by recency, which is the only ordering
    available for threads no ranking pass ever scored."""
    p = params or _DEFAULT_PARAMS
    # Stage-timing probe (fail-soft, None when nobody installed one). The embed
    # arm's cold bit is sampled at entry: an available-but-unloaded embedder means
    # this query pays the tens-of-seconds load inside the request — the cold-model
    # tail the usage ledger exists to name. The cross-encoder's bit is NOT sampled
    # here, because "available and not loaded" is its permanent resting state
    # whenever re-rank is off; it is set at the re-rank itself, where a load would
    # actually be paid (see :meth:`_probe.SearchProbe`).
    probe = _probe.current()
    if probe is not None:
        from . import embed as _embed_cold

        probe.embed_cold = _embed_cold.is_available() and not _embed_cold.is_loaded()
    since_r = resolve_relative_date(since) if since else None
    until_r = resolve_relative_date(until) if until else None

    if agents is not None and agents not in ("exclude", "include", "only"):
        raise ValueError("agents must be 'exclude', 'include', or 'only'")
    if group is not None and group not in ("thread", "browse", "nested", "dup", "none"):
        raise ValueError("group must be 'thread', 'browse', 'nested', 'dup', or 'none'")
    # 'oldest' is the only sort — relevance is the unnamed default. Rejected rather
    # than ignored because the plausible guesses ('newest', 'recent') are asks for a
    # *chronological* answer, and silently serving relevance order answers "when was
    # this last discussed" with "what matched best", which reads as a real answer.
    if sort is not None and sort != "oldest":
        raise ValueError("sort must be 'oldest' or None (relevance)")
    if match not in fts.MATCH_MODES:
        raise ValueError("match must be 'token' or 'substring'")
    page = max(1, int(page))
    # The deepest row this call must be able to return, and where its page starts.
    # Every page slices one ordering built to this depth (see the docstring) —
    # pages that reshuffle each other are worse than no pagination at all, because
    # the caller can't tell a moved row from a missing one.
    depth = page * limit
    offset = depth - limit
    # An explicit types list is the raw thread-type scope; the agents switch
    # stands down so types=['system'] just works without a second knob.
    agents_eff = "include" if types else (agents or "exclude")

    # A thread scope arrives as a ref — a ULID, a legacy integer id, or a
    # provider session id. Resolve it to the thread's id once, up front; an
    # unresolvable ref matches nothing.
    if thread_id is not None:
        with use_session(session) as s:
            thread_id = resolve_thread_ref(s, thread_id)
        if thread_id is None:
            return []

    # A topic scope resolves to the topic's member conversations (cited or linked)
    # and rides the same id-set filter in both arms. A topic with no members — or
    # a non-topic id — matches nothing rather than silently searching everything.
    # A resolved id-set scope may arrive from the caller (the commit scope resolves
    # to one there, because its verdict has to be rendered beside the results).
    # A caller-supplied list arrives ranked (the commit scope ranks by share of the
    # commit), so its order is part of the answer and survives to the browse.
    caller_ranked = thread_ids is not None
    if thread_ids is not None:
        thread_ids = list(thread_ids)
        if not thread_ids:
            return []
    if topic_id is not None:
        from .._knowledge.read import topic_thread_ids

        try:
            members = topic_thread_ids(topic_id, session=session)
        except ValueError:
            return []
        # Two scopes compose by intersection, never by replacement — a caller that
        # named both meant both.
        thread_ids = members if thread_ids is None else [t for t in thread_ids if t in set(members)]
        if not thread_ids:
            return []

    # Empty query (and no prefix scan) → browse: a thread-granular list view.
    # The structural filters compose; ranking/context machinery doesn't apply.
    if not (query or "").strip() and startswith is None:
        return browse_threads(
            limit=limit, since=since_r, until=until_r, source=source, types=types,
            agents=agents or "exclude",
            thread_id=thread_id, thread_ids=thread_ids, path=path, path_ops=path_ops,
            preserve_order=caller_ranked, oldest_first=sort == "oldest",
            page=page, session=session,
        )

    is_count = output == "count"
    # startswith is structural — there's no lexical MATCH to rank or embed
    # against, so the semantic arm and the weighted ranker both sit out. So do
    # sort='oldest' and count: the vector arm returns similarity-ranked nearest
    # neighbours, which can't strengthen a chronological first-mention scan or a
    # tally of literal matches, only pollute them.
    structural = startswith is not None or sort == "oldest" or is_count
    # One row per thread for the ranked shape (rank.group_by_thread): folds
    # annotate the surviving row instead of spending result slots on repeats.
    # Deliberate scopes stand it down — a thread_id scope wants every hit — and
    # count/linkable are ungrouped by shape ('count' already tallies per thread,
    # 'linkable' links every event). group='none' turns it off; group='dup'
    # keeps per-thread hits and folds only cross-thread duplicate content.
    #
    # The list shapes ('browse'/'nested') are the exception to all of that: asking
    # for one is an explicit request for a thread-granular view, so it outranks
    # the shape-based suppressions above. Only output='count' still wins, via the
    # is_count guard on the block that applies the fold.
    listing = group in ("browse", "nested")
    grouping = group != "none" and (
        listing or (not structural and thread_id is None and output is None)
    )
    # Candidate pool depth. 200 (not depth*5) because reachability dies at the pool
    # boundary: for a high-frequency term over a ~1M-doc index, a relevant-but-old
    # hit past bm25's top-N is unreachable no matter how the ranker weighs it. The
    # pool is cheap (one indexed FTS scan + one matvec); ranking 200 is microseconds.
    # Deliberately independent of ``page``: the pool's *composition* decides the
    # ordering — the coherence re-rank scores a thread's community against the
    # pool's mass — so a pool that grew per page would hand each page a different
    # ordering to slice, and a walk would repeat rows and skip others (measured:
    # 122 duplicates over a 721-thread walk). One pool per (query, limit) means one
    # ordering, and every page is a slice of it. What that costs is depth: the
    # ranked shapes reach only pool-deep, which is why they report their total as a
    # floor. ``group='browse'`` is unbounded by it — the exact set supplies whatever
    # the pool never reached.
    over = max(limit, COUNT_FETCH_CAP) if is_count else max(limit * 5, p.pool_floor)

    terms = _rank.search_terms(query)

    fused = retrieve_pool(
        query, over=over, structural=structural, params=p, match_mode=match,
        thread_id=thread_id, thread_ids=thread_ids, content_types=content_types,
        exclude_content_types=exclude_content_types, since=since_r, until=until_r,
        tool_name=tool_name, source=source, types=types, agents=agents_eff, path=path,
        # Strict matching for count and oldest: the OR tier would inflate a tally
        # with partial matches, and in a chronological sort an older partial match
        # would leapfrog the true first mention.
        startswith=startswith, oldest_first=sort == "oldest",
        or_fallback=not (is_count or sort == "oldest"),
        embedder=embedder, session=session,
    )

    did_rerank = False
    if is_count:
        ranked = fused  # whole match pool, unranked — the renderer tallies it
    elif sort == "oldest":
        # Hits with no parseable timestamp sort LAST — an empty key would sort
        # before every real date and crowd the head with undatable hits.
        ranked = sorted(
            fused,
            key=lambda r: (r.get("occurred_at") is None, str(r.get("occurred_at") or "")),
        )
    elif structural:
        ranked = fused  # structural recency order from the scan; the final cut caps it
    else:
        # Weighted lexical rank. Grouping ranks the whole pool — folded rows must
        # backfill from ranked candidates past `limit`, and sorting the pool costs
        # microseconds either way; otherwise rank just what the cut needs, with a
        # wider head when a cross-encoder re-rank may re-order it.
        if reranker is None:
            from . import rerank as _rerank_mod

            reranker = _rerank_mod.default()
        do_rerank = _do_rerank(query, terms, rerank, reranker, p.rerank_auto)
        if grouping:
            rank_to = len(fused)
        else:
            rank_to = max(depth, p.rerank_pool) if do_rerank else depth
        _t_rank = perf_counter()
        ranked = _rank.rank_search_results(fused, terms, rank_to, params=p)
        _probe.record("rank_ms", _t_rank)
        # Result-side half of the gate: when the ranked head is a strong literal
        # match the lexical order is trustworthy and the cross-encoder stands down
        # — it exists for the vocab-mismatch case, and re-ranking a confident head
        # costs seconds only to degrade it (see head_is_strong). The exception is a
        # message head that merely echoes the query verbatim (a pasted, unanswered
        # question): it does not earn the stand-down, so the cross-encoder gets to
        # look for a differently-worded answer below it — but its verdict is trusted
        # only when it rescues one (echo_head, applied after scoring below). An
        # explicit rerank=True skips this check along with the shape gate.
        echo_head = rerank is not True and _rank.head_is_query_echo(ranked, terms)
        if do_rerank and rerank is not True and _rank.head_earns_standdown(ranked, terms):
            do_rerank = False
        # Cross-encoder head re-rank (gated, fail-soft): scores (query, content)
        # jointly and floats the true target up. None → keep lexical order. Head
        # only (RERANK_POOL): the cross-encoder's cost is per document, and past
        # the head the lexical order is only backfill.
        if do_rerank:
            # The head is at least `limit` deep: a result that will be displayed
            # must be one the cross-encoder actually scored, so a strong-but-sparse
            # hit sitting at position `rerank_pool`+1 (a long answering doc the
            # density scorer buries) is not permanently unreachable at the pool
            # boundary. Normally limit ≤ rerank_pool, so the head is rerank_pool.
            # Sized from limit and not from the page depth for the same reason the
            # pool is: re-ranking a deeper head on page 2 would re-order page 1.
            head_n = max(p.rerank_pool, limit)
            head, tail = ranked[:head_n], ranked[head_n:]
            # Score the match-centred window (plus a long doc's head/tail), not the
            # doc head alone — a hit whose relevant text sits mid-message, or whose
            # answer sits far past an incidental query term, would otherwise be
            # scored on its intro.
            # Sampled here rather than at entry: this is the point past which a
            # cross-encoder load is definitely paid, so the flag names a real cost
            # instead of an installed-but-idle model.
            if probe is not None:
                from . import rerank as _rerank_cold

                probe.rerank_cold = (
                    _rerank_cold.is_available() and not _rerank_cold.is_loaded()
                )
            _t0 = perf_counter()
            reordered = reranker.rerank(
                query, head,
                get_text=lambda r: _rank.rerank_windows(
                    r.get("full_content") or r.get("snippet") or "",
                    terms, p.rerank_doc_chars,
                ),
            )
            if probe is not None:
                probe.rerank_ms += (perf_counter() - _t0) * 1000.0
            if reordered is not None:
                # An echo-licensed re-rank (the head was a strong verbatim query
                # echo, not a weak head) is trusted only when it actually rescues a
                # vocab-mismatch hit — a new top that is itself a strong lexical
                # match means the cross-encoder merely reshuffled confident
                # candidates, the degrade case the stand-down protects against.
                if echo_head and _rank.head_is_strong(reordered[:1], terms):
                    pass  # keep the trustworthy lexical order
                else:
                    ranked, did_rerank = reordered + tail, True
        # Community-coherence re-rank from the corpus-native embedding graph
        # (default on — measured recall lift at every depth on the log-mined
        # protocol; see embed_graph). Only when the cross-encoder stood down:
        # the two are alternative head-orderers, and running coherence under
        # the re-rank reshuffles which candidates reach its scoring window —
        # measured end-to-end, that stack loses the recall the arm alone buys.
        if not did_rerank:
            from . import embed as _embed

            # Coherence is a semantic-arm refinement built from event_vectors; with
            # the embed arm off (a core install, or THREAD_ARCHIVE_EMBED=off) there
            # are no vectors to build its graph from, so skip it rather than kick a
            # build that probes a table that isn't there. The graph primitives stay
            # embed-agnostic for tests and direct callers; only the search path gates.
            if _embed.is_available():
                _t_coh = perf_counter()
                ranked = _apply_coherence(ranked, p.coherence_gamma)
                _probe.record("coherence_ms", _t_coh)

    # A pool that came back short of what it asked for holds the WHOLE match set:
    # nothing was cut, so the shaped rows below are already the total and no extra
    # query could add a row. Measured on the pool's raw reach, not its length —
    # dedup shrinks the list, so length would call a saturated pool short and
    # report a cut as a complete answer, which is the one error that matters here.
    pool_saturated = getattr(fused, "raw", len(fused)) >= over
    exhaustive = not pool_saturated
    capped = False

    if not is_count:
        # Every row-shaped output collapses same-anchor twins (a thread-meta
        # title/summary doc and the first event it anchors to — one anchor,
        # two rows that open identically in thread_read).
        _t_group = perf_counter()
        ranked = _rank.collapse_same_anchor(ranked)
        if grouping:
            if group == "nested":
                # Clusters before the cut, and caps itself by thread — so the cut
                # can't slice a thread's cluster in half.
                ranked = _rank.cluster_by_thread(ranked, max_threads=depth)
            elif group == "browse":
                ranked = _rank.group_by_thread(ranked, fold_duplicates=False)
                # The fold and the reconciliation below have unrelated cost models —
                # one scales with the pool, the other with the whole match set — so
                # the group clock closes here and reopens past the extend rather
                # than billing both to one number.
                _probe.record("group_ms", _t_group)
                _t_extend = perf_counter()
                # The exhaustive shape: the match set decides membership and the
                # pool only orders it, whether or not the pool saturated.
                # Unconditional because the reconciliation is what makes the set
                # page-independent — gating it on saturation would mean a query's
                # own totals shifted the moment it outgrew the pool.
                ranked, capped = _extend_with_unranked_threads(
                    ranked, query, match_mode=match, startswith=startswith,
                    oldest_first=sort == "oldest", session=session,
                    scope=dict(
                        thread_id=thread_id, thread_ids=thread_ids,
                        tool_name=tool_name, path=path, types=types,
                        content_types=content_types,
                        exclude_content_types=exclude_content_types,
                        source=source, since=since_r, until=until_r,
                        agents=agents_eff,
                    ),
                )
                _probe.record("extend_ms", _t_extend)
                _t_group = perf_counter()
                exhaustive = not capped
            elif group == "dup":
                ranked = _rank.fold_duplicate_threads(ranked)
            else:
                ranked = _rank.group_by_thread(ranked)
        _probe.record("group_ms", _t_group)

    # What this page is a page OF. The shaped row count is the honest total
    # whenever the pool held everything; past that only the browse shape resolves
    # its true set, so the others report the pool's reach and say so via
    # ``exhaustive=False`` rather than passing a cut off as a total.
    total = len(ranked)
    total_threads = len({r["thread_id"] for r in ranked}) if not is_count else None

    if is_count:
        hits = ranked
    elif grouping and group == "nested":
        # Nested counts in threads, so its page is a window over *clusters* — a
        # row slice would cut a thread's hits in half across two pages.
        hits = _nested_page(ranked, offset, limit)
    else:
        hits = ranked[offset:offset + limit]

    # Per-hit enrichments the renderer reads. A pure tally (count) needs none.
    _t_enrich = perf_counter()
    if not is_count:
        if grouping and listing and group is not None:
            # The list shapes render thread rows, so they need the thread columns.
            # 'browse' shows no message at all, so its per-hit match window would
            # be computed only to be discarded.
            for r in hits:
                r["_group"] = group
            _enrich_thread_rows(hits, session=session)
            if group == "browse":
                context_lines, context_events = 0, None
        if context_lines > 0:
            for r in hits:
                if r.get("full_content"):
                    r["context"] = extract_context_lines(r["full_content"], query, context_lines)
        if context_events:
            cb, ca, cts = parse_context_events_spec(context_events)
            ctx_map = get_context_events(hits, cb, ca, cts, session=session)
            for r in hits:
                if r["event_id"] in ctx_map:
                    r["context_events"] = ctx_map[r["event_id"]]
        # The quality verdict (Feature: match-signal) turns on whether the
        # cross-encoder actually ran, so carry it onto each hit for the renderer.
        for r in hits:
            r["_did_rerank"] = did_rerank

    _enrich_thread_titles(hits, session=session)
    _probe.record("enrich_ms", _t_enrich)
    if probe is not None:
        probe.did_rerank = did_rerank
    # The list shapes count in threads, so that is the number their pages divide;
    # every other shape counts in rows. A tally (output='count') is not a page of
    # anything — it already reports over the whole pool — so it carries no
    # pagination facts rather than misleading ones.
    if is_count:
        return Results(hits, page=1)
    unit = total_threads if listing else total
    return Results(
        hits, total=total, total_threads=total_threads, capped=capped, page=page,
        pages=max(1, (unit + limit - 1) // limit) if unit is not None else None,
        exhaustive=exhaustive,
    )


__all__ = [
    "SearchParams",
    "search",
    "retrieve_pool",
    "pool_cache",
    "read_thread",
    "read_thread_structured",
    "resolve_thread_ref",
    "rebuild_fts",
    "index_events",
    "index_thread_meta",
    "ensure_fts",
    "fts_status",
    "format_results",
    "search_events",
    "blame_path",
    "blame_commit",
    "thread_files",
    "refresh_code_index",
    "rebuild_code_index",
    "code_index_status",
]
