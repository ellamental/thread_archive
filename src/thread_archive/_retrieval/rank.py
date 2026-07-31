"""Result ranking — the weighted lexical scorer.

The production ranker. The federation produces a pool; this turns it into an order:

  1. :func:`dedup_results` collapses byte-identical hits (same logical message
     re-emitted under two event_ids) before ranking.
  2. :func:`rank_search_results` scores each hit by term **density** (normalized by
     content length), **phrase proximity**, **recency**, **cross-backend fusion**
     (the ``_rrf`` agreement score the vector arm contributes), each arm's own
     **magnitude**, and a **content-type** multiplier over the sum.

The weights come from :class:`.params.SearchParams` (see that module for the
production values); content-type from
``_CONTENT_TYPE_WEIGHT`` (user > text > tool …). An alternative
configuration is another ``SearchParams`` instance passed down from
``search(params=...)`` — the seam a measured candidate rides.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

from ._types import EventHit
from .params import DEFAULT as _DEFAULT_PARAMS
from .params import SearchParams

# Per-content-type relevance multiplier — user messages are the most intentional,
# tool/thinking the noisiest. A title is aboutness itself, so it ranks with user
# text.
_CONTENT_TYPE_WEIGHT = {
    "title": 1.5,
    "user": 1.5,
    "text": 1.2,
    "tool_result": 0.8,
    "tool": 0.5,
    "thinking": 0.3,
    "continuation_summary": 0.1,
}

# Function words carry no relevance signal, so they're dropped from the *ranking*
# term set (density, phrase, the K/N match-quality verdict) — otherwise "how did we
# fix the auth bug" scores hits on "how/did/we/the" and the quality header inflates.
# The FTS MATCH itself is untouched (bm25 already discounts common terms); this only
# shapes what the scorer and the trust signal count. An all-stopword query keeps its
# terms — better a weak signal than none.
_STOPWORDS = frozenset(
    "a an and are as at be but by did do does for from had has have how i in is it "
    "its me my of on or our so that the their then there these this to was we "
    "were what when where which who why will with you your".split()
)


def recency_score(occurred_at, now: datetime | None = None,
                  half_life_hours: float = _DEFAULT_PARAMS.recency_half_life_hours) -> int:
    """0–20 exponential-decay recency score (half-life ~3 days by default).
    Datetime or ISO string in, int out."""
    dt = _parse_naive_dt(occurred_at)
    if dt is None:
        return 0
    if now is None:
        # occurred_at is stored and parsed as naive-UTC (see _parse_naive_dt), so
        # the clock we diff against must be naive-UTC too — a naive-local now() would
        # skew every event's age by the local UTC offset.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    age_hours = max(0, (now - dt).total_seconds() / 3600)
    return max(1, int(20 * math.exp(-age_hours / half_life_hours)))


def _parse_naive_dt(val) -> datetime | None:
    if not val:
        return None
    try:
        dt = val if isinstance(val, datetime) else datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _drop_stopwords(terms: list[str]) -> list[str]:
    """Filter function words out of a ranking term set; keep the set intact when
    filtering would empty it (an all-stopword query still needs *some* signal).
    Multi-word phrase terms (from quoted spans) always survive."""
    kept = [t for t in terms if " " in t or t not in _STOPWORDS]
    return kept or terms


#: Characters that can only be furniture at a ranking term's edge — sentence
#: punctuation, quotes and brackets, the FTS prefix marker. Stripped from both
#: ends of every single-word term, and only from the ends: interior punctuation is
#: what makes ``foo.bar`` and ``0.45`` one term rather than two.
#:
#: Left on, an edge character makes a term match *nothing*. :func:`_term_pattern`
#: anchors both ends on word boundaries, and a boundary after a non-word character
#: needs a word character next to it — so ``thread,`` matches ``thread,x`` and not
#: the ``thread,`` of ordinary prose. The term is dead: it scores no density on any
#: document, including one holding the query verbatim, and it drags down the
#: term-hit count that decides the rendered ``quality=strong`` verdict. It
#: also slips the term past :data:`_STOPWORDS`, so ``this,`` survives where ``this``
#: is dropped and a corpus-wide word joins both the ranking set and the OR union.
_TERM_EDGE = ",.;:!?*\"'()[]{}<>…“”‘’"


def _edge_stripped(terms: list[str]) -> list[str]:
    """``terms`` with :data:`_TERM_EDGE` furniture off both ends, empties dropped.
    Phrase terms (from quoted spans) are handed over untouched — a quoted span is
    verbatim by definition, punctuation and all."""
    out = []
    for t in terms:
        stripped = t if " " in t else t.strip(_TERM_EDGE)
        if stripped:
            out.append(stripped)
    return out


def search_terms(query: str) -> list[str]:
    """Lowercased ranking terms for ``query`` — the term set the density/phrase
    scorer matches against. Identifier-style words (``help_think``) are preserved
    intact; quoted spans become one phrase term each; AND/OR/NOT/pipe and
    function words (:data:`_STOPWORDS`) are dropped, as is the edge punctuation a
    natural-language query carries (:data:`_TERM_EDGE`)."""
    if not query or not query.strip():
        return []
    # Backticks are markdown fencing a user wraps around an identifier
    # (`thread_search`), never part of the token. Strip them so the density scorer
    # credits the bare identifier wherever it appears, not only backtick-wrapped
    # occurrences — the FTS tokenizer already ignores them; this aligns ranking.
    query = query.replace("`", " ")
    if re.search(r'\b(AND|OR|NOT)\b|".*?"|\*$', query):
        phrases = [m.strip().lower() for m in re.findall(r'"([^"]+)"', query) if m.strip()]
        outside = re.sub(r'"[^"]*"', " ", query)
        words = [w.lower() for w in outside.split() if w not in ("AND", "OR", "NOT", "|")]
        terms = phrases + _edge_stripped(words)
        if not terms:
            terms = _edge_stripped(
                [t.lower() for t in query.split() if t not in ("AND", "OR", "NOT")]
            )
        return _drop_stopwords(terms)
    q = re.sub(r"(?<=\w)-(?=\w)", " ", query)
    q = re.sub(r"[:\^()\[\]{}]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return _drop_stopwords(_edge_stripped([t.lower() for t in q.split()]))


# Common English inflectional suffixes an indexed word may carry past a ranking
# term's stem — 'caches' for 'cache', 'tokens' for 'token', 'cached' for 'cache'.
# A ranking term matches its word plus at most one of these, bounded by word
# edges, so density credits inflected forms (the porter FTS index already
# retrieved them) without counting an unrelated word that merely *starts* with
# the term: 'auth' must land on 'auth'/'auths' but never inside 'author'.
_TERM_SUFFIX = "(?:s|es|ed|d|ing|ion|ions|ly)?"


def _term_pattern(term: str) -> re.Pattern:
    """A word-boundaried matcher for one ranking term. A term ≥4 chars also
    accepts a trailing inflection (:data:`_TERM_SUFFIX`); shorter terms match the
    bare word only, so 'go' can't reach 'going'. Both ends are anchored on word
    boundaries, so a term never scores inside a longer unrelated word."""
    core = re.escape(term)
    body = core + _TERM_SUFFIX if len(term) >= 4 else core
    return re.compile(r"\b" + body + r"\b")


def term_hit_count(content: str, terms: list[str]) -> int:
    """How many of ``terms`` appear in ``content`` as words (:func:`_term_pattern` —
    a ≥4-char term also matches its common inflections, but never inside a longer
    unrelated word). Each term counts at most once."""
    if not terms or not content:
        return 0
    c = content.lower()
    return sum(1 for t in terms if _term_pattern(t).search(c))


def strong_match_floor(n_terms: int) -> int:
    """Term-hit floor for a *strong* lexical match: ⌈2/3·N⌉, min 1. What the
    renderer's ``quality=strong`` verdict calls trustworthy."""
    return max(1, -(-2 * n_terms // 3))


def dedup_results(results: list[EventHit]) -> list[EventHit]:
    """Collapse byte-identical hits before ranking. The same logical message can
    land as two events (streaming re-emits a text block), which the
    (event_id, content_type) federation dedup misses. Key on (thread_id, content)
    so identical short lines in *different* threads still both surface; keep first."""
    if len(results) <= 1:
        return results
    seen: set[tuple] = set()
    out: list[EventHit] = []
    for r in results:
        key = (r.get("thread_id"), (r.get("full_content") or r.get("snippet") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def collapse_same_anchor(results: list[EventHit]) -> list[EventHit]:
    """Collapse hits sharing one ``(thread_id, event_id)`` anchor. A thread-meta
    doc (title) is anchored to its thread's first indexed event, so it
    and that event can both match one query — two rows that open identically in
    ``thread_read``. Runs post-rank, order-preserving: the better-placed row
    survives."""
    if len(results) <= 1:
        return results
    seen: set[tuple] = set()
    out: list[EventHit] = []
    for r in results:
        key = (r.get("thread_id"), r.get("event_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# Runs of digits are the volatile token of a near-duplicate flood — the counter,
# run index, or timestamp that makes each of a fleet of otherwise-identical
# messages (routine ops, a re-asked question, a pending-todo restatement, a swarm
# of agents on one templated prompt) byte-distinct. Folding every digit run to one
# placeholder gives the near-copies a single identity, so a thread emitting twenty
# copies of one status line counts as having said it once.
_VOLATILE_DIGITS = re.compile(r"\d+")


def _norm_content(r: EventHit) -> str:
    """Whitespace-collapsed, lowercased, digit-folded hit content — the
    near-duplicate identity :func:`score_features` counts a thread's evidence by.
    Two hits whose text differs only in its digit runs (a counter, a run index, a
    timestamp) share one identity, so a fleet of near-copies is recognizable as one
    piece of content however many times it was emitted."""
    text = " ".join((r.get("full_content") or r.get("snippet") or "").split()).lower()
    return _VOLATILE_DIGITS.sub("#", text)


#: One doc's ranking features, in the order :func:`score_from_features` weights
#: them: the eight weighted signals, then the content-type multiplier applied to
#: their sum.
ScoreFeatures = tuple[
    float, float, float, float, float, float, float, float, float]


def score_features(
    results: list[EventHit],
    terms: list[str],
    *,
    params: Optional[SearchParams] = None,
    now: datetime | None = None,
) -> list[ScoreFeatures]:
    """Per-doc ``(density, phrase, recency, fusion, bm25, bm25_score, semantic,
    thread_evidence, ct_weight)`` — the half of scoring that the ranking *weights*
    do not touch.

    The split is a measurement seam. Regex term matching over every doc's full
    text dominates the scorer's cost, and it is identical for every configuration
    sharing ``density_norm_chars``, ``recency_half_life_hours``,
    ``content_type_weights``, and ``now`` — which is every configuration a weight
    sweep visits. Extracted, a candidate configuration reduces to
    :func:`score_from_features`: arithmetic over rows a harness computed once.

    ``now`` anchors the recency decay and is the one input a caller comparing
    configurations must pin: left to the clock it drifts between runs, so two
    otherwise-identical scorings taken far enough apart do not agree exactly.

    The semantic feature is the vector arm's cosine **spread across this pool**
    (min-max, so the pool's nearest neighbour reads 1.0 and its most distant 0.0),
    not the raw cosine. Raw, the signal is nearly all constant: an embedder's
    cosines over a candidate pool sit in a narrow band, so the weighted term would
    contribute a large offset and a small gradient — and the offset is not even
    neutral, because the content-type multiplier scales the whole sum, which would
    turn a flat semantic term into a content-type preference amplifier. Spread
    across the pool, what the weight buys is the ordering the arm actually
    expresses. A hit the vector arm never returned reads 0.0, exactly as it
    already does for the lexical features.

    ``thread_evidence`` is the one feature that reads the pool rather than the
    doc: how many **distinct** matches the pool holds from this hit's thread,
    log-damped and normalized against the thread that carries the most. Every other
    signal scores a single event on its own merits, so a conversation that returns
    to a subject twenty times places exactly like one that mentioned it once in
    passing — nothing else in the scorer can see that the thread is the subject's
    home. Repetition is evidence of what a thread is *about*, which is what a
    subject-shaped query asks for.

    Two things keep it evidence rather than a length prior. Matches are counted by
    :func:`_norm_content` identity — a digit-folded near-duplicate key — so a
    thread emitting twenty copies of one status line
    counts once; the corpus's routine floods are the threads a raw count would
    reward most. And the count is log-damped, so the chattiest thread on a subject
    cannot outweigh a specific conversation that a query names outright.
    """
    p = params or _DEFAULT_PARAMS
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)  # naive-UTC, matching occurred_at
    ct_weights = p.content_type_weights if p.content_type_weights is not None else _CONTENT_TYPE_WEIGHT
    term_patterns = {t: _term_pattern(t) for t in terms}
    full_phrase = " ".join(terms)

    sims = [s for s in (r.get("_semantic") for r in results) if s is not None]
    sim_lo = min(sims) if sims else 0.0
    sim_span = (max(sims) - sim_lo) if sims else 0.0

    # Counted only when something weighs it: the near-duplicate identity is a
    # regex pass over every candidate's full text, which is the scorer's dominant
    # cost, and the shipped weight is 0.0.
    thread_hits: dict[str, int] = {}
    ev_peak = 0.0
    if p.thread_evidence_weight:
        distinct: dict[str, set[str]] = {}
        for r in results:
            tid = r.get("thread_id")
            if tid:
                distinct.setdefault(tid, set()).add(_norm_content(r))
        thread_hits = {tid: len(seen) for tid, seen in distinct.items()}
        ev_peak = math.log1p(max(thread_hits.values()) - 1) if thread_hits else 0.0

    features: list[ScoreFeatures] = []
    for result in results:
        content = (result.get("full_content", "") or "").lower()
        content_len = max(len(content), 1)
        matches = {t: term_patterns[t].search(content) for t in terms}
        term_count = sum(1 for m in matches.values() if m)
        density = term_count / max(1, content_len / p.density_norm_chars)

        phrase_bonus = 0.0
        if len(terms) >= 2:
            if full_phrase in content:
                phrase_bonus = 3.0
            else:
                positions = [m.start() for m in matches.values() if m]
                if len(positions) == len(terms):
                    span = max(positions) - min(positions)
                    if span < 100:
                        phrase_bonus = 2.0
                    elif span < 300:
                        phrase_bonus = 1.0

        recency = recency_score(result.get("occurred_at", ""), now,
                                half_life_hours=p.recency_half_life_hours)
        sim = result.get("_semantic")
        semantic = ((sim - sim_lo) / sim_span) if (sim is not None and sim_span > 0) else 0.0
        evidence = (
            math.log1p(thread_hits.get(result.get("thread_id") or "", 1) - 1) / ev_peak
            if ev_peak > 0 else 0.0
        )
        features.append((
            density, phrase_bonus, recency,
            result.get("_rrf", 0.0) or 0.0,
            result.get("_lex", 0.0) or 0.0,
            result.get("_bm25", 0.0) or 0.0,
            semantic, evidence,
            ct_weights.get(result.get("content_type") or "", 1.0),
        ))
    return features


def score_from_features(
    features: list[ScoreFeatures], params: Optional[SearchParams] = None,
) -> list[float]:
    """The combined relevance score per doc — a configuration's entire
    contribution to the ranking, given :func:`score_features` rows.

    Only the *ratios* between the weights matter: scaling them all by a
    constant scales every score and leaves the order untouched (the content-type
    multiplier distributes over the sum), which is why ``density_weight`` reads
    as the anchor the rest are calibrated against."""
    p = params or _DEFAULT_PARAMS
    return [
        (density * p.density_weight + phrase * p.phrase_weight
         + recency * p.recency_weight + fusion * p.fusion_weight
         + bm25 * p.bm25_weight + bm25_score * p.bm25_score_weight
         + semantic * p.semantic_weight + evidence * p.thread_evidence_weight) * ct_weight
        for (density, phrase, recency, fusion, bm25, bm25_score, semantic, evidence,
             ct_weight) in features
    ]


def rank_search_results(
    results: list[EventHit],
    terms: list[str],
    limit: int,
    *,
    params: Optional[SearchParams] = None,
    now: datetime | None = None,
) -> list[EventHit]:
    """Re-rank ``results`` by term density, phrase proximity, recency, content-type,
    and cross-backend fusion (``_rrf``). The production scorer; every weight comes
    from ``params`` (default: the shipped configuration, :data:`.params.DEFAULT` —
    see that module for the evidence). Returns the top ``limit``.

    Ties break on the pool's incoming order, so the ranking inherits the
    federation's ordering rather than an arbitrary one."""
    scores = score_from_features(
        score_features(results, terms, params=params, now=now), params)
    order = sorted(range(len(results)), key=lambda i: (-scores[i], i))
    return [results[i] for i in order[:limit]]
