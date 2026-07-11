"""Search + read over the SQLite store.

``search`` runs the production pipeline: federate two arms — FTS5 **lexical** + an
optional in-process **vector** (semantic) search — fuse them by reciprocal-rank
fusion (``_rrf`` normalized to [0,1]), dedup, score with the weighted lexical
**ranker** (density / phrase / recency / content-type / fusion — :mod:`.rank`),
then optionally re-order the head with an in-process **cross-encoder** (:mod:`.rerank`)
on conceptual multi-term queries. With no ``[embeddings]`` extra the vector and
cross-encoder arms sit out and search is lexical-only (still through the ranker).
``read_thread`` reconstructs a conversation; ``rebuild_fts`` is the FTS half of reindex.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..store import Thread, use_session
from . import rank as _rank
from ._classify import resolve_relative_date
from ._context import extract_context_lines, get_context_events, parse_context_events_spec
from .format import COUNT_FETCH_CAP, format_results
from .fts import ensure_fts, fts_status, index_events, index_thread_meta, rebuild_fts, search_events
from .read import read_thread, read_thread_structured, resolve_thread_ref

logger = logging.getLogger(__name__)


def _enrich_thread_titles(hits: list[dict], *, session: Optional[Session] = None) -> None:
    """Fill ``thread_title`` on hits from the threads table (one query)."""
    if not hits:
        return
    ids = {h["thread_id"] for h in hits}
    with use_session(session) as s:
        rows = s.execute(select(Thread.id, Thread.title, Thread.name).where(Thread.id.in_(ids))).all()
    titles = {tid: (title or name) for tid, title, name in rows}
    for h in hits:
        h["thread_title"] = titles.get(h["thread_id"])


def _rrf_merge(result_lists: list[list[dict]], limit: int, k: int = 60) -> list[dict]:
    """Reciprocal-rank fusion of several ranked hit lists, keyed by
    (event_id, content_type). RRF score = Σ 1/(k + rank), **normalized to [0,1]**
    (÷ peak) so the ranker's ``fusion_weight`` is calibrated against it. The semantic
    provenance (``_semantic`` cosine) is carried onto the fused hit."""
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


def _semantic_hits(query, *, thread_id, content_types, exclude_content_types, since, until, over, source):
    """The vector arm — None when the extra is absent, nothing's indexed, or embed fails."""
    try:
        from . import vectors

        if not vectors.is_available():
            return None
        return vectors.search(
            query, thread_id=thread_id, content_types=content_types,
            exclude_content_types=exclude_content_types, limit=over, since=since, until=until,
            source=source,
        )
    except Exception:  # noqa: BLE001 — vector arm must never break lexical search
        return None


# A throwaway conceptual, multi-term query for the warm pass: multi-term + no operators so
# it trips the rerank gate (should_rerank), exercising the cross-encoder head too.
_WARM_QUERY = "warm up the retrieval vector index and reranker"


def warm_models() -> None:
    """Prime the whole retrieval pipeline on the caller's thread so the FIRST real search
    doesn't pay startup costs *inside* the request. Those costs — cold-loading the embedding
    + cross-encoder models (tens of seconds), loading the vector matrix off disk, and the
    first cross-encoder inference — otherwise land on the first query and can exceed an MCP
    client's request timeout (see :mod:`thread_archive.mcp.server`, which calls this on a
    background thread at startup).

    Two steps: load the models explicitly (works even with an empty store), then run one
    throwaway conceptual search to fill the process-global caches the first real query
    reuses (the vector matrix, the reranker's warmed inference path). Fail-soft throughout:
    a missing ``[embeddings]`` extra, a load failure, or an unavailable store just leaves
    search to cold-load lazily, exactly as before."""
    from . import embed as _embed
    from . import rerank as _rerank

    for stage in (_embed.warm, _rerank.warm):
        try:
            stage()
        except Exception:  # noqa: BLE001 — warming is best-effort; never raise into a caller
            logger.debug("warm_models: a model stage failed to preload", exc_info=True)

    # Run one throwaway search end to end: it loads the vector matrix and runs a first
    # cross-encoder inference, both of which cache process-globally for the real queries.
    # Scope it to the agent surface's default (mcp.server's DEFAULT_SEARCH_CONTENT_TYPES),
    # so the matrix this primes is keyed the same as the real queries reuse (the matrix
    # cache is keyed by content-type scope; a mismatched scope would prime a matrix the
    # real query never touches).
    try:
        from .. import api

        api.search(_WARM_QUERY, limit=1, content_types=["user", "title", "summary"])
    except Exception:  # noqa: BLE001 — a store that isn't ready just warms the models, not the caches
        logger.debug("warm_models: dummy warm search skipped", exc_info=True)


def _do_rerank(query: str, terms: list[str], force: Optional[bool]) -> bool:
    """Whether to run the cross-encoder head re-rank. ``force`` (the ``rerank=``
    arg) overrides the auto-gate; otherwise gate to conceptual multi-term queries
    *and* an available reranker (the ``[embeddings]`` extra)."""
    from . import rerank as _rerank

    if force is not None:
        return force and _rerank.is_available()
    return _rank.should_rerank(query, terms) and _rerank.is_available()


def search(
    query: str,
    *,
    limit: int = 20,
    thread_id: Optional[int] = None,
    content_types: Optional[list[str]] = None,
    exclude_content_types: Optional[list[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tool_name: Optional[str] = None,
    source: Optional[list[str]] = None,
    startswith: Optional[str] = None,
    sort: Optional[str] = None,
    output: Optional[str] = None,
    context_lines: int = 2,
    context_events: Optional[str] = None,
    rerank: Optional[bool] = None,
    session: Optional[Session] = None,
) -> list[dict]:
    """Search over conversation events through the production pipeline: lexical FTS5
    + optional semantic vectors → RRF fusion → dedup → weighted lexical rank →
    optional cross-encoder head re-rank. Returns event-hit dicts with the thread
    title enriched. ``since``/``until`` accept ISO timestamps or a relative ``<N>d``
    window; ``source`` restricts to threads of the named provider(s); ``rerank``
    forces the cross-encoder stage on/off (else auto-gated).

    ``startswith`` does a structural prefix scan (query text unused). ``sort='oldest'``
    returns the candidate pool chronologically, bypassing the ranker. ``output='count'``
    returns the whole match pool unranked (the renderer tallies per-thread). With a
    structural shape (browse / startswith / oldest / count) the semantic arm and the
    cross-encoder sit out. ``context_lines`` (default 2; 0 = the raw FTS snippet)
    attaches a numbered window around each hit's match; ``context_events`` (``N`` /
    ``b:a`` / ``b:a:types``) attaches the neighbouring events. Both enrich the
    returned hits in place (skipped for count)."""
    since_r = resolve_relative_date(since) if since else None
    until_r = resolve_relative_date(until) if until else None

    # browse/startswith are structural — there's no lexical MATCH to rank or embed
    # against, so the semantic arm and the weighted ranker both sit out.
    structural = startswith is not None or not (query or "").strip()
    is_count = output == "count"
    # Candidate pool depth. 200 (not limit*5) because reachability dies at the pool
    # boundary: for a high-frequency term over a ~1M-doc index, a relevant-but-old
    # hit past bm25's top-N is unreachable no matter how the ranker weighs it. The
    # pool is cheap (one indexed FTS scan + one matvec); ranking 200 is microseconds.
    over = max(limit, COUNT_FETCH_CAP) if is_count else max(limit * 5, 200)

    terms = _rank.search_terms(query)

    lexical = search_events(
        query, thread_id=thread_id, content_types=content_types,
        exclude_content_types=exclude_content_types, limit=over,
        since=since_r, until=until_r, tool_name=tool_name, source=source,
        startswith=startswith, session=session,
    )
    semantic = None if structural else _semantic_hits(
        query, thread_id=thread_id, content_types=content_types,
        exclude_content_types=exclude_content_types, since=since_r, until=until_r,
        over=over, source=source,
    )

    # Fuse the arms into a wide pool (keeps _rrf agreement + _semantic provenance),
    # then dedup byte-identical hits before ranking.
    fused = _rrf_merge([lexical, semantic], over) if semantic else lexical
    fused = _rank.dedup_results(fused)

    did_rerank = False
    if is_count:
        ranked = fused  # whole match pool, unranked — the renderer tallies it
    elif sort == "oldest":
        ranked = sorted(fused, key=lambda r: str(r.get("occurred_at") or ""))[:limit]
    elif structural:
        ranked = fused[:limit]  # structural recency order from the scan
    else:
        # Weighted lexical rank; a wider pool when a cross-encoder re-rank will
        # re-order the head, else straight to `limit`.
        do_rerank = _do_rerank(query, terms, rerank)
        rank_to = max(limit, _rank.RERANK_POOL) if do_rerank else limit
        ranked = _rank.rank_search_results(fused, terms, rank_to)
        # Cross-encoder head re-rank (gated, fail-soft): scores (query, content)
        # jointly and floats the true target up. None → keep lexical order.
        if do_rerank:
            from . import rerank as _rerank

            # Score the match-centred window, not the doc head — a long hit whose
            # relevant text sits mid-message would otherwise be scored on its intro.
            reordered = _rerank.rerank(
                query, ranked,
                get_text=lambda r: _rank.match_window(
                    r.get("full_content") or r.get("snippet") or "",
                    terms, _rerank.RERANK_DOC_CHARS,
                ),
            )
            if reordered is not None:
                ranked, did_rerank = reordered, True

    hits = ranked if is_count else ranked[:limit]

    # Per-hit enrichments the renderer reads. A pure tally (count) needs none.
    if not is_count:
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
    return hits


__all__ = [
    "search",
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
