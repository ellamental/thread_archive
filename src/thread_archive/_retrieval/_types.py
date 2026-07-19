"""Shared shapes of the retrieval data plane.

The event hit is the one record every retrieval stage passes along — built by
``fts.build_event_hit``, fused (``_rrf``/``_semantic``), ranked, enriched
(``thread_title``, ``context``, ``context_events``), and finally rendered by
``format.format_results``. Typing it here lets the checker carry the shape
across those modules instead of prose alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import NotRequired, Optional, TypedDict


class EventHit(TypedDict):
    """One search hit, in the canonical shape shared by every pipeline stage.

    The required keys are what ``build_event_hit`` constructs. The optional ones
    are stage annotations: ``_semantic`` (vector-arm cosine, carried through
    fusion), ``_rrf`` (normalized fusion score), ``_did_rerank`` (whether the
    cross-encoder re-ordered the head — drives the renderer's quality verdict),
    ``context`` (±N-line window around the match), ``context_events``
    (neighbouring events, ``{"before": [...], "after": [...]}``), and the
    grouping annotations ``_thread_more`` (further hits in this thread folded
    into this row) / ``_dup_thread_ids`` (other threads whose hit carried the
    same content, folded into this row — see ``rank.group_by_thread``).

    A *browse* row (empty-query search — see :mod:`.browse`) rides the same
    shape with ``_browse=True``: one row per thread, ``event_id`` = the
    thread's newest event, plus ``thread_source`` / ``n_events`` for the
    list renderer.

    A keyword search asked for a thread-granular list (``search(group=…)``)
    carries ``_group`` naming the shape, the same ``thread_source`` /
    ``n_events`` columns, and — under ``group='nested'``, whose clustering
    replaces ranked order with per-thread event order — ``_rank_pos``, so
    ``format.top_hit`` can still find the head the quality verdict judges.

    Two further keys are stamped by the web layer when it shapes hits for the
    viewer's JSON: ``term_hits`` (how many query terms literally appear in the
    hit, for the per-hit K/N badge) and ``dup_threads`` (``_dup_thread_ids``
    resolved to ``{thread_id, title}`` so the fold renders as names)."""

    event_id: int
    thread_id: int
    thread_title: Optional[str]
    event_type: str
    content_type: Optional[str]
    snippet: str
    full_content: str
    occurred_at: Optional[datetime]
    _semantic: NotRequired[float]
    _rrf: NotRequired[float]
    _did_rerank: NotRequired[bool]
    _thread_more: NotRequired[int]
    _dup_thread_ids: NotRequired[list[int]]
    context: NotRequired[str]
    context_events: NotRequired[dict]
    _browse: NotRequired[bool]
    _group: NotRequired[str]
    _rank_pos: NotRequired[int]
    thread_source: NotRequired[Optional[str]]
    n_events: NotRequired[int]
    term_hits: NotRequired[int]
    dup_threads: NotRequired[list[dict]]
