"""Shared shapes of the retrieval data plane.

The event hit is the one record every retrieval stage passes along — built by
``fts.build_event_hit``, fused (``_rrf``/``_semantic``), ranked, enriched
(``thread_title``, ``context``, ``context_events``), and finally rendered by
``format.format_results``. Typing it here lets the checker carry the shape
across those modules instead of prose alone.

:class:`Results` is the list of those hits plus what a *page* of them needs to
describe itself — how many matched in total, which page this is. It subclasses
``list`` so every existing caller, test, and eval that treats a search result as
a plain list keeps working unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import NotRequired, Optional, TypedDict


class EventHit(TypedDict):
    """One search hit, in the canonical shape shared by every pipeline stage.

    The required keys are what ``build_event_hit`` constructs. The optional ones
    are stage annotations: ``_semantic`` (vector-arm cosine, carried through
    fusion), ``_lex`` (the lexical arm's normalized reciprocal rank — bm25 for a
    MATCH pass), ``_bm25`` (that arm's own bm25 score, peak-normalized over the
    pool; absent on a hit no MATCH pass scored),
    ``_rrf`` (normalized fusion score),
    ``context`` (±N-line window around the match) and ``context_events``
    (neighbouring events, ``{"before": [...], "after": [...]}``).

    A *browse* row (empty-query search — see :mod:`.browse`) rides the same
    shape with ``_browse=True``: one row per thread, ``event_id`` = the
    thread's newest event, plus ``thread_source`` / ``n_events`` for the
    list renderer.

    ``_browse_order`` marks a browse whose rows are NOT in last-activity order —
    the caller ranked them (the commit scope ranks by share of the commit) — so the
    renderer's header can say which ordering it is showing.

    A browse scoped by ``path`` (the code axis) carries the ``_path_*`` columns
    instead of describing the thread's size: what it did to that file
    (``_path_ops``), the window of touches (``_path_first`` / ``_path_last``), how
    many matching files it touched (``_path_files``, with ``_path_sample`` naming
    the first of them) — and its ``event_id`` is re-pointed at the strongest, newest
    touch, so opening the row lands on the work rather than on the thread's tail.

    ``term_hits`` is stamped by the web layer when it shapes hits for the viewer's
    JSON: how many query terms literally appear in the hit, for the K/N badge."""

    event_id: int
    thread_id: str
    thread_title: Optional[str]
    event_type: str
    content_type: Optional[str]
    snippet: str
    full_content: str
    occurred_at: Optional[datetime]
    _semantic: NotRequired[float]
    _lex: NotRequired[float]
    _bm25: NotRequired[float]
    _rrf: NotRequired[float]
    context: NotRequired[str]
    context_events: NotRequired[dict]
    _browse: NotRequired[bool]
    thread_source: NotRequired[Optional[str]]
    n_events: NotRequired[int]
    _browse_order: NotRequired[str]
    _path_ops: NotRequired[dict[str, int]]
    _path_first: NotRequired[Optional[str]]
    _path_last: NotRequired[Optional[str]]
    _path_files: NotRequired[int]
    _path_sample: NotRequired[list[str]]
    term_hits: NotRequired[int]


class Pool(list):
    """The fused candidate pool, plus how many rows the arms actually returned.

    ``raw`` is that count *before* fusion and dedup collapsed it. It is the only
    thing that answers "did the pool hit its depth" — ``len()`` cannot, because
    dedup removes rows, so a pool that scanned to its boundary and then folded
    half of it away is indistinguishable by length from one that ran out of
    matches. That difference is exactly the one a caller needs to know whether
    its result is the whole set or a cut of it.
    """

    __slots__ = ("raw",)

    def __init__(self, hits=(), *, raw: int = 0) -> None:
        super().__init__(hits)
        self.raw = raw


class Results(list):
    """A page of hits, and the facts that make it a *page* rather than an answer.

    Search returns a cut, and for most of this pipeline's life the cut was the
    only thing a caller saw: ten rows, with nothing to distinguish "these are all
    of them" from "these are ten of nine hundred". That ambiguity is what makes a
    ranked search unusable as an enumeration — not the ordering of the tail, but
    that the tail is invisible. These fields close it.

    - ``total`` / ``total_threads`` — the size of the match **set**, not of this
      page. ``None`` where the shape can't know it cheaply.
    - ``capped`` — the totals are floors: the set scan stopped at
      :data:`~.fts.SET_SCAN_CAP` (render them as ``N+``, never as ``N``).
    - ``page`` / ``pages`` — where this page sits, and how many there are.
      ``pages`` is ``None`` when ``total`` is.
    - ``exhaustive`` — every matched thread is reachable by paging. True only for
      the shapes resolved from the exact set (see ``fts.matched_threads``) rather
      than cut from the candidate pool.

    A plain ``list`` subclass on purpose: slicing, iteration, ``len``, and
    equality all behave as before, so nothing downstream needs to know this type
    exists to keep working. Attributes are read with ``getattr(hits, 'total',
    None)`` by the renderer, which also handles the plain lists that ``rank`` and
    the test helpers construct.
    """

    __slots__ = ("total", "total_threads", "capped", "page", "pages", "exhaustive")

    def __init__(
        self,
        hits=(),
        *,
        total: Optional[int] = None,
        total_threads: Optional[int] = None,
        capped: bool = False,
        page: int = 1,
        pages: Optional[int] = None,
        exhaustive: bool = False,
    ) -> None:
        super().__init__(hits)
        self.total = total
        self.total_threads = total_threads
        self.capped = capped
        self.page = page
        self.pages = pages
        self.exhaustive = exhaustive
