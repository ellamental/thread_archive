"""Result ranking — the weighted lexical scorer + the cross-encoder gate.

The production ranker. The federation produces a pool; this turns it into an order:

  1. :func:`dedup_results` collapses byte-identical hits (same logical message
     re-emitted under two event_ids) before ranking.
  2. :func:`rank_search_results` scores each hit by term **density** (normalized by
     content length), **phrase proximity**, **recency**, **content-type** weight,
     and **cross-backend fusion** (the ``_rrf`` agreement score the vector arm
     contributes) — the same five knobs, at the same shipped weights, as prod.
  3. :func:`should_rerank` gates the latency-bearing cross-encoder head re-rank
     (:mod:`.rerank`) to *conceptual* multi-term queries — the vocab-mismatch ones
     where the bi-encoder ranks the target mid-list. Keyword shapes (OR / quoted /
     identifier-dominated / single-term) the lexical arm already nails are skipped.
     :func:`head_is_strong` is the second, result-side half of that gate: once the
     pool is ranked, a top hit that literally contains the query terms means the
     lexical order is already trustworthy and the re-rank stands down (see
     :func:`thread_archive._retrieval.search`).

The weights come from :class:`.params.SearchParams` (see that module for the
production values and their evidence); content-type from
``_CONTENT_TYPE_WEIGHT`` (user > text > tool_result …). An alternative
configuration is another ``SearchParams`` instance passed down from
``search(params=...)`` — the seam the search lab experiments ride.
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
# text. A stored summary is *derived* — a keyword-dense digest whose
# short length already wins the density term, so an at-parity multiplier lets
# summaries crowd verbatim evidence out of the top ranks and puts generated prose
# above the record it summarizes. The discount keeps summaries findable (they are
# the only docs carrying synthesis vocabulary that never appears verbatim) while
# making them yield to any primary source that matches comparably.
_CONTENT_TYPE_WEIGHT = {
    "title": 1.5,
    "user": 1.5,
    "text": 1.2,
    "tool_result": 0.8,
    "summary": 0.6,
    "tool": 0.5,
    "thinking": 0.3,
    "continuation_summary": 0.1,
}

# Cross-encoder re-rank pool at the shipped configuration — how many ranked
# candidates feed the reranker before cutting to ``limit`` (rationale in
# params.py). SearchParams.rerank_pool is the per-call override.
RERANK_POOL = _DEFAULT_PARAMS.rerank_pool

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


def search_terms(query: str) -> list[str]:
    """Lowercased ranking terms for ``query`` — the term set the density/phrase
    scorer matches against. Identifier-style words (``help_think``) are preserved
    intact; quoted spans become one phrase term each; AND/OR/NOT/pipe and
    function words (:data:`_STOPWORDS`) are dropped."""
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
        terms = phrases + words
        if not terms:
            terms = [t.lower() for t in query.split() if t not in ("AND", "OR", "NOT")]
        return _drop_stopwords(terms)
    q = re.sub(r"(?<=\w)-(?=\w)", " ", query)
    q = re.sub(r"[:\^()\[\]{}]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return _drop_stopwords([t.lower() for t in q.split()])


_IDENTIFIER_RE = re.compile(r"[_]|::|(?<=\w)\.(?=\w)")


def should_rerank(query: str, terms: list[str]) -> bool:
    """Gate the cross-encoder re-rank to *conceptual* multi-term queries. Skip the
    keyword shapes the lexical arm already nails: pipe-OR, quoted exact phrases,
    identifier-dominated queries, and single-term queries. A query that merely
    *mentions* an identifier inside a conceptual question ("why thread_search
    misses old threads") still reranks — only when identifier terms make up half
    or more of the terms is the lexical arm trusted outright. A false positive
    only costs latency (the re-rank is fail-soft), never results."""
    q = (query or "").strip()
    if "|" in q or '"' in q:
        return False
    if len(terms) < 2:
        return False
    code_terms = sum(1 for t in terms if _IDENTIFIER_RE.search(t))
    return code_terms * 2 < len(terms)


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
    boundaries — the substring era let 'auth' score inside 'author'."""
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
    """Term-hit floor for a *strong* lexical match: ⌈2/3·N⌉, min 1. The one
    threshold shared by the renderer's ``quality=strong`` verdict and the
    :func:`head_is_strong` re-rank skip — what the header calls trustworthy is
    exactly what the pipeline trusts."""
    return max(1, -(-2 * n_terms // 3))


def head_is_strong(hits: list[EventHit], terms: list[str]) -> bool:
    """Whether the top-ranked hit is a strong literal match for the query
    (:func:`strong_match_floor` of the terms land in its content). A strong head
    means the lexical ranking already found the target's vocabulary — the
    cross-encoder exists for the *vocab-mismatch* case, and re-ranking a
    confident lexical order costs seconds only to shuffle it (measured on the
    title-query eval: forced re-rank drops MRR 0.55→0.48)."""
    if not hits or not terms:
        return False
    text = hits[0].get("full_content") or hits[0].get("snippet") or ""
    return term_hit_count(text, terms) >= strong_match_floor(len(terms))


# Aboutness docs: a title/summary the query matches names what the thread
# *is*, the trustworthy-order case the strong-head stand-down was measured on.
ABOUTNESS_CONTENT_TYPES = frozenset({"title", "summary"})


def head_is_query_echo(hits: list[EventHit], terms: list[str]) -> bool:
    """Whether the strong ranked head is a *message* that echoes the whole query
    verbatim as one contiguous phrase — a pasted prompt, a quoted ticket, a
    restated-but-unanswered question. Such a head is as plausibly the question as
    the answer, so (unlike a title/summary the query matches) it does not
    by itself make the lexical order trustworthy: the cross-encoder is let run to
    look for a differently-worded answer below it, but its verdict is trusted only
    when it actually rescues one (see :func:`thread_archive._retrieval.search`)."""
    if len(terms) < 2 or not head_is_strong(hits, terms):
        return False
    top = hits[0]
    if (top.get("content_type") or "") in ABOUTNESS_CONTENT_TYPES:
        return False
    content = (top.get("full_content") or top.get("snippet") or "").lower()
    return " ".join(terms) in content


def head_earns_standdown(hits: list[EventHit], terms: list[str]) -> bool:
    """Whether a strong ranked head should stand the cross-encoder down. A strong
    head (:func:`head_is_strong`) normally means the lexical order is trustworthy,
    so the re-rank stands down — except a verbatim query echo
    (:func:`head_is_query_echo`), which earns no such trust."""
    return head_is_strong(hits, terms) and not head_is_query_echo(hits, terms)


def match_window(content: str, terms: list[str], chars: int) -> str:
    """The ~``chars``-wide slice of ``content`` centred on the *densest* term
    cluster — what a cross-encoder should score. Feeding it the doc *head*
    mis-scores any hit whose relevant text sits mid-message, and centring on the
    *earliest* term drifts to a stray incidental mention when the answering
    passage — where the query terms actually gather — is further down. The window
    is placed over the passage covering the most distinct query terms (ties →
    earliest), plus a third of the window as lead-in. Head of the doc when nothing
    matches (conceptual queries may share no literal term with the target)."""
    if not content or len(content) <= chars:
        return content
    low = content.lower()
    pos = _densest_cluster_pos(low, terms, chars)
    if pos <= chars // 3:  # no match, or the cluster is already inside a head window
        return content[:chars]
    start = min(pos - chars // 3, len(content) - chars)
    return content[start:start + chars]


def _densest_cluster_pos(low: str, terms: list[str], chars: int) -> int:
    """The start position of the ``chars``-wide window over ``low`` (already
    lowercased) covering the most distinct ``terms`` — a two-pointer sweep over
    every term occurrence. ``-1`` when nothing matches, so the caller falls back
    to the doc head."""
    occ = sorted(
        (m.start(), t) for t in set(terms) for m in _term_pattern(t).finditer(low)
    )
    if not occ:
        return -1
    best_pos, best_cover = occ[0][0], 0
    freq: dict[str, int] = {}
    left = 0
    for right_pos, right_term in occ:
        freq[right_term] = freq.get(right_term, 0) + 1
        while right_pos - occ[left][0] >= chars:
            lt = occ[left][1]
            freq[lt] -= 1
            if not freq[lt]:
                del freq[lt]
            left += 1
        if len(freq) > best_cover:
            best_cover, best_pos = len(freq), occ[left][0]
    return best_pos


def rerank_windows(content: str, terms: list[str], chars: int) -> list[str]:
    """The passages a cross-encoder should score for one hit — its relevance is
    the best of them (MaxP). A doc that fits in ``chars`` is one passage. A longer
    doc adds its head and tail alongside the match-centred window, so an answering
    passage that sits far from the query terms — a resolution at the very end, or
    a doc whose only near-query text is an incidental mention up top — is scored
    rather than truncated away. Duplicates (a short-enough doc, an already-head
    window) collapse."""
    content = content or ""
    if len(content) <= chars:
        return [content]
    out: list[str] = []
    for w in (match_window(content, terms, chars), content[:chars], content[-chars:]):
        if w and w not in out:
            out.append(w)
    return out


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
    doc (title/summary) is anchored to its thread's first indexed event, so it
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
# placeholder gives the near-copies a single identity, so the cross-thread fold
# collapses the flood to one representative instead of letting dozens of them fill
# the ranked window and push out the distinct thread the query actually wants.
_VOLATILE_DIGITS = re.compile(r"\d+")


def _norm_content(r: EventHit) -> str:
    """Whitespace-collapsed, lowercased, digit-folded hit content — the cross-thread
    near-duplicate identity ``group_by_thread`` and ``fold_duplicate_threads`` fold
    on. Two hits whose text differs only in its digit runs (a counter, a run index,
    a timestamp) share one identity, so a flood of near-copies collapses to a single
    row rather than monopolizing the result window."""
    text = " ".join((r.get("full_content") or r.get("snippet") or "").split()).lower()
    return _VOLATILE_DIGITS.sub("#", text)


def fold_duplicate_threads(results: list[EventHit]) -> list[EventHit]:
    """Fold *only* cross-thread duplicate content, leaving every surviving
    thread's own hits intact — the half of :func:`group_by_thread` that removes
    redundancy without also collapsing a thread to a single row.

    A hit whose content is near-identical (:func:`_norm_content` — equal once
    digit runs are folded) to a row already on screen from a **different** thread
    folds into that row's ``_dup_thread_ids``. Repeats *within* one thread survive:
    a reader paging a result list wants the thread's several matches laid out,
    where an agent spending result slots wants the one representative row.

    Ranked order in, ranked order out. A thread whose hit folds here can still
    appear on a later hit of its own that nothing else duplicates."""
    by_content: dict[str, EventHit] = {}
    out: list[EventHit] = []
    for r in results:
        norm = _norm_content(r)
        dup = by_content.get(norm) if norm else None
        if dup is not None and dup.get("thread_id") != r.get("thread_id"):
            ids = dup.setdefault("_dup_thread_ids", [])
            if r.get("thread_id") not in ids:
                ids.append(r.get("thread_id"))
            continue
        if norm and dup is None:
            by_content[norm] = r
        out.append(r)
    return out


def group_by_thread(results: list[EventHit], *, fold_duplicates: bool = True) -> list[EventHit]:
    """Collapse a ranked hit list to one row per thread, annotated instead of
    truncated — result slots are an agent's budget, and redundancy spends them:

    - further hits in an already-represented thread fold into its row's
      ``_thread_more`` count (drill in with a ``thread_id``-scoped search);
    - a hit whose content is near-identical (:func:`_norm_content` — equal once
      digit runs are folded) to a row already on screen from a *different*
      thread — a forked session, a fleet of spawned agents carrying one prompt,
      a flood of routine near-copies differing only by a run index — folds into
      that row's ``_dup_thread_ids`` instead of repeating the content. A thread
      folded this way can still surface later on a distinct hit of its own.

    ``fold_duplicates=False`` keeps the per-thread collapse but drops that second
    fold, so **every** matched thread keeps a row — what a thread *list* owes its
    reader, where a ranked result list owes its reader brevity.

    Ranked order in, ranked order out: a thread ranks where its best hit ranks.
    """
    by_thread: dict[str, EventHit] = {}
    by_content: dict[str, EventHit] = {}
    out: list[EventHit] = []
    for r in results:
        tid = r.get("thread_id")
        rep = by_thread.get(tid)
        if rep is not None:
            rep["_thread_more"] = rep.get("_thread_more", 0) + 1
            continue
        norm = _norm_content(r) if fold_duplicates else ""
        if norm:
            dup = by_content.get(norm)
            if dup is not None:
                ids = dup.setdefault("_dup_thread_ids", [])
                if tid not in ids:
                    ids.append(tid)
                continue
        by_thread[tid] = r
        if norm:
            by_content[norm] = r
        out.append(r)
    return out


# Per-thread hit budget for the nested shape. A nested render is bounded by
# threads, not hits, so one sprawling thread must not eat the whole view: past
# this many hits a thread's remainder folds into its cluster's ``_thread_more``
# (drill in with a thread_id-scoped search, which is never grouped).
NESTED_HITS_PER_THREAD = 5


def cluster_by_thread(
    results: list[EventHit],
    *,
    max_threads: int,
    max_per_thread: int = NESTED_HITS_PER_THREAD,
) -> list[EventHit]:
    """Reorder a ranked hit list so each thread's hits sit together — the nested
    shape: every match kept, laid out under the thread it came from.

    Threads keep their ranked order (a thread sits where its best hit ranked) and
    the first ``max_threads`` of them survive; within a thread the hits go back to
    **event order**, since a thread's matches read as a sequence, not a ranking.
    Hits past ``max_per_thread`` fold into the cluster's leading row as
    ``_thread_more``. Unlike :func:`group_by_thread` no cross-thread duplicate
    fold runs: a nested view enumerates what matched.
    """
    order: list[str] = []
    buckets: dict[str, list[EventHit]] = {}
    overflow: dict[str, int] = {}
    for pos, r in enumerate(results):
        tid = r.get("thread_id")
        if tid not in buckets:
            if len(buckets) >= max_threads:
                continue
            buckets[tid] = []
            order.append(tid)
        bucket = buckets[tid]
        if len(bucket) >= max_per_thread:
            overflow[tid] = overflow.get(tid, 0) + 1
            continue
        # Clustering destroys the ranked order this list arrived in, and the
        # match-quality verdict is a statement about the TOP-ranked hit — so each
        # surviving hit carries where it ranked (see format._top_hit).
        r["_rank_pos"] = pos
        bucket.append(r)

    out: list[EventHit] = []
    for tid in order:
        bucket = sorted(buckets[tid], key=lambda h: h.get("event_id") or 0)
        if overflow.get(tid):
            bucket[0]["_thread_more"] = overflow[tid]
        out.extend(bucket)
    return out


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
    signal scores a single event, and grouping then represents a thread by its best
    one — so a conversation that returns to a subject twenty times ranks exactly
    like one that mentioned it once in passing, on whichever of its events happened
    to score highest. Repetition is evidence of what a thread is *about*, which is
    what a subject-shaped query asks for.

    Two things keep it evidence rather than a length prior. Matches are counted by
    :func:`_norm_content` identity, the same digit-folded near-duplicate key the
    cross-thread fold uses, so a thread emitting twenty copies of one status line
    counts once — the corpus's routine floods are the threads a raw count would
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
