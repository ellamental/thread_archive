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
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .._store import Event, Thread, use_session
from . import _probe, pool_cache
from . import embed_graph as _embed_graph
from . import rank as _rank
from ._classify import resolve_relative_date
from ._context import extract_context_lines, get_context_events, parse_context_events_spec
from ._types import EventHit
from .browse import browse_threads
from .format import COUNT_FETCH_CAP, format_results
from .fts import ensure_fts, fts_status, index_events, index_thread_meta, rebuild_fts, search_events
from .params import DEFAULT as _DEFAULT_PARAMS
from .params import SearchParams
from .read import read_thread, read_thread_structured, resolve_thread_ref

logger = logging.getLogger(__name__)


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
                   source, thread_ids=None, agents="exclude", embedder=None):
    """The vector arm — None when the extra is absent, nothing's indexed, or embed fails."""
    try:
        from . import vectors

        if not vectors.is_available():
            return None
        return vectors.search(
            query, thread_id=thread_id, content_types=content_types,
            exclude_content_types=exclude_content_types, limit=over, since=since, until=until,
            source=source, thread_ids=thread_ids, agents=agents, embedder=embedder,
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

    Two steps: load the models explicitly (works even with an empty store), then run one
    throwaway conceptual search to fill the process-global caches the first real query
    reuses (the vector matrix, the reranker's warmed inference path). ``embedder`` and
    ``reranker`` are the models to prime (default: the process ones). Fail-soft
    throughout: a missing ``[embeddings]`` extra, a load failure, or an unavailable store
    just leaves search to cold-load lazily, exactly as before."""
    if embedder is None:
        from . import embed as _embed

        embedder = _embed.default()
    if reranker is None:
        from . import rerank as _rerank

        reranker = _rerank.default()

    for stage in (embedder, reranker):
        try:
            stage.warm()
        except Exception:  # noqa: BLE001 — warming is best-effort; never raise into a caller
            logger.debug("warm_models: a model stage failed to preload", exc_info=True)

    # Run one throwaway search end to end: it loads the vector matrix and runs a first
    # cross-encoder inference, both of which cache process-globally for the real queries.
    # Scope it to the agent surface's default (mcp.server's DEFAULT_SEARCH_CONTENT_TYPES),
    # so the matrix this primes is keyed the same as the real queries reuse (the matrix
    # cache is keyed by content-type scope; a mismatched scope would prime a matrix the
    # real query never touches).
    # Build the corpus graph inline while we're already off the request path —
    # the coherence re-rank serves from this cache and never builds during a
    # search (a stale graph refreshes in the background; the FIRST build is
    # the warm pass's job).
    if _embed_graph.coherence_gamma() > 0.0:
        try:
            _embed_graph.get(block=True)
        except Exception:  # noqa: BLE001 — warming is best-effort
            logger.debug("warm_models: corpus graph build skipped", exc_info=True)

    try:
        from .. import _api as api

        # rerank=True: the point is priming the cross-encoder's inference path, so
        # force it past the gates (a strong-headed warm hit would otherwise skip it).
        api.search(_WARM_QUERY, limit=1, content_types=["user", "title", "summary"], rerank=True)
    except Exception:  # noqa: BLE001 — a store that isn't ready just warms the models, not the caches
        logger.debug("warm_models: dummy warm search skipped", exc_info=True)


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
    oldest_first: bool = False,
    or_fallback: bool = True,
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
    sort, count), which sit the vector arm out.
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
        )
        cached = cache.get(key)
        if cached is not None:
            if probe is not None:
                probe.pool_size = len(cached)
            return cached

    _t0 = perf_counter()
    lexical = search_events(
        query, thread_id=thread_id, thread_ids=thread_ids, content_types=content_types,
        exclude_content_types=exclude_content_types, limit=over,
        since=since, until=until, tool_name=tool_name, source=source,
        types=types, agents=agents,
        startswith=startswith, oldest_first=oldest_first,
        or_fallback=or_fallback, session=session,
    )
    if probe is not None:
        probe.fts_ms += (perf_counter() - _t0) * 1000.0

    # A tool_name scope also sits the vector arm out: tool docs aren't embedded
    # (only user/text/title/summary are), so every semantic hit in a tool-scoped
    # search would be a hit the filter should have excluded. A types scope sits
    # it out too: vectors carry no thread-type filter, so its hits could leak
    # threads the filter excludes.
    _t0 = perf_counter()
    semantic = None if structural or tool_name or types else _semantic_hits(
        query, thread_id=thread_id, content_types=content_types,
        exclude_content_types=exclude_content_types, since=since, until=until,
        over=over, source=source, thread_ids=thread_ids, agents=agents,
        embedder=embedder,
    )
    if probe is not None:
        probe.semantic_ms += (perf_counter() - _t0) * 1000.0

    # Fuse the arms into a wide pool (keeps _rrf agreement + _semantic provenance),
    # then dedup byte-identical hits before ranking.
    fused = _rrf_merge([lexical, semantic], over, k=p.rrf_k) if semantic else lexical
    fused = _rank.dedup_results(fused)
    if probe is not None:
        probe.pool_size = len(fused)
    if cache is not None and key is not None:
        cache.put(key, fused)
    return fused


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
    sort: Optional[str] = None,
    group: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
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
    row-shaped output, grouped or not."""
    p = params or _DEFAULT_PARAMS
    # Stage-timing probe (fail-soft, None when nobody installed one). ``cold`` is
    # sampled at entry: an available-but-unloaded model means the first query to
    # reach that arm pays the tens-of-seconds load inside the request — the
    # cold-model tail the usage ledger exists to name.
    probe = _probe.current()
    if probe is not None:
        from . import embed as _embed_cold
        from . import rerank as _rerank_cold

        probe.cold = (_embed_cold.is_available() and not _embed_cold.is_loaded()) or (
            _rerank_cold.is_available() and not _rerank_cold.is_loaded()
        )
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
    thread_ids: Optional[list[str]] = None
    if topic_id is not None:
        from .._knowledge.read import topic_thread_ids

        try:
            thread_ids = topic_thread_ids(topic_id, session=session)
        except ValueError:
            return []
        if not thread_ids:
            return []

    # Empty query (and no prefix scan) → browse: a thread-granular list view.
    # The structural filters compose; ranking/context machinery doesn't apply.
    if not (query or "").strip() and startswith is None:
        return browse_threads(
            limit=limit, since=since_r, until=until_r, source=source, types=types,
            agents=agents or "exclude",
            thread_id=thread_id, thread_ids=thread_ids,
            oldest_first=sort == "oldest", session=session,
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
    # Candidate pool depth. 200 (not limit*5) because reachability dies at the pool
    # boundary: for a high-frequency term over a ~1M-doc index, a relevant-but-old
    # hit past bm25's top-N is unreachable no matter how the ranker weighs it. The
    # pool is cheap (one indexed FTS scan + one matvec); ranking 200 is microseconds.
    over = max(limit, COUNT_FETCH_CAP) if is_count else max(limit * 5, p.pool_floor)

    terms = _rank.search_terms(query)

    fused = retrieve_pool(
        query, over=over, structural=structural, params=p,
        thread_id=thread_id, thread_ids=thread_ids, content_types=content_types,
        exclude_content_types=exclude_content_types, since=since_r, until=until_r,
        tool_name=tool_name, source=source, types=types, agents=agents_eff,
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
            rank_to = max(limit, p.rerank_pool) if do_rerank else limit
        ranked = _rank.rank_search_results(fused, terms, rank_to, params=p)
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
            head_n = max(p.rerank_pool, limit)
            head, tail = ranked[:head_n], ranked[head_n:]
            # Score the match-centred window (plus a long doc's head/tail), not the
            # doc head alone — a hit whose relevant text sits mid-message, or whose
            # answer sits far past an incidental query term, would otherwise be
            # scored on its intro.
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
                ranked = _apply_coherence(ranked, p.coherence_gamma)

    if not is_count:
        # Every row-shaped output collapses same-anchor twins (a thread-meta
        # title/summary doc and the first event it anchors to — one anchor,
        # two rows that open identically in thread_read).
        ranked = _rank.collapse_same_anchor(ranked)
        if grouping:
            if group == "nested":
                # Clusters before the cut, and caps itself by thread — so the cut
                # can't slice a thread's cluster in half.
                ranked = _rank.cluster_by_thread(ranked, max_threads=limit)
            elif group == "browse":
                ranked = _rank.group_by_thread(ranked, fold_duplicates=False)
            elif group == "dup":
                ranked = _rank.fold_duplicate_threads(ranked)
            else:
                ranked = _rank.group_by_thread(ranked)

    if is_count or (grouping and group == "nested"):
        hits = ranked
    else:
        hits = ranked[:limit]

    # Per-hit enrichments the renderer reads. A pure tally (count) needs none.
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
    if probe is not None:
        probe.did_rerank = did_rerank
    return hits


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
]
