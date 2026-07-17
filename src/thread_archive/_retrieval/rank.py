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

The weights are the production values, with their rationale: recency 1.0 (the
corpus skews to OLD threads, so a strong recency boost buries what users actually
read), fusion 50.0 (the MRR optimum for the fused lexical+vector ranking),
content-type from ``_CONTENT_TYPE_WEIGHT`` (user > text > tool_result …).
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from ._types import EventHit

# Per-content-type relevance multiplier — user messages are the most intentional,
# tool/thinking the noisiest. A title is aboutness itself, so it ranks with user
# text. A stored summary is *derived* — a keyword-dense librarian digest whose
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

# Cross-backend RRF fusion weight — the MRR optimum once the vector arm joined the
# federation (the fusion-sweep: 50.0 Pareto-dominates 0.0 on R@1/10/20 and MRR).
# Lexical scoring is ~0 for a semantic-only hit, so without this term a
# vocab-mismatch hit the vector arm surfaced would sink regardless of its rank.
_SEARCH_FUSION_WEIGHT = 50.0

# Recency weight 1.0 — the corpus skews to OLD threads, so a strong recency boost
# buries what users actually read; 1.0 keeps a mild recent tiebreaker.
_SEARCH_RECENCY_WEIGHT = 1.0

# Cross-encoder re-rank pool — how many ranked candidates to feed the reranker
# before cutting to ``limit``. Wide enough to cover recall@20, small enough to keep
# the in-process re-rank stage quick.
RERANK_POOL = 24

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


def recency_score(occurred_at, now: datetime | None = None) -> int:
    """0–20 exponential-decay recency score (half-life ~3 days). Datetime or ISO
    string in, int out."""
    dt = _parse_naive_dt(occurred_at)
    if dt is None:
        return 0
    if now is None:
        # occurred_at is stored and parsed as naive-UTC (see _parse_naive_dt), so
        # the clock we diff against must be naive-UTC too — a naive-local now() would
        # skew every event's age by the local UTC offset.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    age_hours = max(0, (now - dt).total_seconds() / 3600)
    return max(1, int(20 * math.exp(-age_hours / 72)))


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


def match_window(content: str, terms: list[str], chars: int) -> str:
    """The ~``chars``-wide slice of ``content`` centred on the earliest term match —
    what a cross-encoder should score. Feeding it the doc *head* mis-scores any hit
    whose relevant text sits mid-message; centring keeps the match (plus a third of
    the window as lead-in) inside the scored span. Head of the doc when nothing
    matches (conceptual queries may share no literal term with the target)."""
    if not content or len(content) <= chars:
        return content
    low = content.lower()
    pos = min((p for p in (low.find(t) for t in terms) if p >= 0), default=-1)
    if pos <= chars // 3:  # no match, or match already inside a head window
        return content[:chars]
    start = min(pos - chars // 3, len(content) - chars)
    return content[start:start + chars]


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


def rank_search_results(
    results: list[EventHit],
    terms: list[str],
    limit: int,
    *,
    recency_weight: float = _SEARCH_RECENCY_WEIGHT,
    density_weight: float = 100.0,
    phrase_weight: float = 50.0,
    fusion_weight: float = _SEARCH_FUSION_WEIGHT,
    content_type_weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> list[EventHit]:
    """Re-rank ``results`` by term density, phrase proximity, recency, content-type,
    and cross-backend fusion (``_rrf``). The production scorer — see the module
    docstring for the weight evidence. Returns the top ``limit``."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)  # naive-UTC, matching occurred_at
    ct_weights = content_type_weights if content_type_weights is not None else _CONTENT_TYPE_WEIGHT
    term_patterns = {t: re.compile(r"\b" + re.escape(t) + r"\b") for t in terms if len(t) < 4}

    def combined_score(result: EventHit) -> float:
        content = (result.get("full_content", "") or "").lower()
        content_len = max(len(content), 1)
        term_count = sum(
            1 for t in terms
            if (len(t) >= 4 and t in content) or (len(t) < 4 and bool(term_patterns[t].search(content)))
        )
        density = term_count / max(1, content_len / 500)

        phrase_bonus = 0.0
        if len(terms) >= 2:
            full_phrase = " ".join(terms)
            if full_phrase in content:
                phrase_bonus = 3.0
            else:
                positions = [content.find(t) for t in terms if content.find(t) >= 0]
                if len(positions) == len(terms):
                    span = max(positions) - min(positions)
                    if span < 100:
                        phrase_bonus = 2.0
                    elif span < 300:
                        phrase_bonus = 1.0

        recency = recency_score(result.get("occurred_at", ""), now)
        ct_weight = ct_weights.get(result.get("content_type") or "", 1.0)
        rrf = result.get("_rrf", 0.0) or 0.0
        return (density * density_weight + phrase_bonus * phrase_weight
                + recency * recency_weight + rrf * fusion_weight) * ct_weight

    ranked = sorted(enumerate(results), key=lambda x: (-combined_score(x[1]), x[0]))
    return [r for _, r in ranked[:limit]]
