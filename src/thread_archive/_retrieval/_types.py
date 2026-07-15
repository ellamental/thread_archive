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
    fusion), ``_topical`` (topic-arm subject-link weight — the strongest subject
    that reached this hit), ``_rrf`` (normalized fusion score), ``_did_rerank``
    (whether the cross-encoder re-ordered the head — drives the renderer's quality
    verdict), ``context`` (±N-line window around the match) and ``context_events``
    (neighbouring events, ``{"before": [...], "after": [...]}``)."""

    event_id: int
    thread_id: int
    thread_title: Optional[str]
    event_type: str
    content_type: Optional[str]
    snippet: str
    full_content: str
    occurred_at: Optional[datetime]
    _semantic: NotRequired[float]
    _topical: NotRequired[float]
    _rrf: NotRequired[float]
    _did_rerank: NotRequired[bool]
    context: NotRequired[str]
    context_events: NotRequired[dict]
