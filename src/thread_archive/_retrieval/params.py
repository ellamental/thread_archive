"""SearchParams — every tunable of the retrieval pipeline in one object.

The production pipeline (:func:`thread_archive._retrieval.search`) reads its
numeric knobs from a :class:`SearchParams`; :data:`DEFAULT` carries the shipped
values, so ``search(query)`` and ``search(query, params=SearchParams())`` are
the same ranking. A candidate configuration is just another instance —
``SearchParams(recency_weight=0.0)`` — passed through ``search(params=...)``
and scored against the incumbent on identical cases by the quality-corpus
harness (``tests/quality_corpus.run_cases(params=...)``).

**What standing these numbers have.** The shipped weights were arrived at on a
gold corpus whose labels were established by *searching this same stack* — a
labeler sweeping the corpus with the ranker under test can only ever mark what
that ranker already reaches, so a systematic blind spot never scores as a miss
and every delta measured that way is an upper bound on itself by an unknown
margin. Those protocols are retired, and with them the per-file deltas that
justified each weight. What is written below is therefore the **mechanism** each
term exists for — which is a property of the scoring function and stays true —
plus the measurements that came from labels somebody else fixed: the external IR
benchmarks. Read the values as *inherited and not currently re-derived*: they are
what ships and what every candidate is scored against, not a configuration the
present bench has confirmed. Re-deriving them is a deliberate run over the public
benchmarks (``search_lab/README.md`` → "Taking a baseline").

- ``fusion_weight`` 400.0 — the cross-backend fusion term (the normalized
  ``_rrf`` agreement score), weighted to compete with density. Density is
  unbounded (matched terms per ``density_norm_chars``), so a short doc carrying a
  few of a long question's common words outscores the fusion term's ceiling
  several times over: a vocab-mismatch answer the vector arm ranks first (high
  ``_rrf``, low density) sinks under lexically dense confounds. Weighting
  cross-arm *agreement* to roughly density's working scale is what keeps it
  reachable — the paraphrase and vague query shapes, where the lexical arm has
  no purchase, are the ones that move. Past ~500 the vector arm starts overriding
  lexical evidence it should defer to and keyword-shaped queries give back recall.
  Saturating density instead (``d/(d+k)``, bounding it to compete on fusion's
  scale) buys the same paraphrase recall and costs more elsewhere: the linear
  term is load-bearing on subject-shaped queries.
- ``bm25_weight`` 100.0 — the weight on ``_lex``, the lexical arm's own placement
  of a hit (peak-normalized reciprocal rank; FTS5 orders by bm25 but never
  surfaces the score, so without this term the arm's verdict survives only as
  the order the pool arrives in). It is the counterweight to density's blind
  spot: density is IDF-blind and length-normalized (matched terms per
  ``density_norm_chars``), weighing a corpus-common term exactly like the rare
  one that discriminates and then dividing by length, so a short doc carrying a
  few common query words outranks the long doc carrying the discriminating ones.
  The SWE-chat hold-out credits the term — +.011 MRR / +.007 nDCG@10 / +.003
  recall@10 pooled, and up on the provenance-labeled ``commit`` file specifically
  — and nothing is tuned against that corpus, so that is the measurement here
  that stands.
  Where it has no substitute is the search ``_rrf`` cannot reach. Fusion runs only when the
  vector arm returns, so a **lexical-only** search — a ``tool_name`` or ``types``
  scope, a structural query, an archive with no embeddings — would otherwise rank
  on density alone. Out of domain, where that path is the whole stack, the gap
  that opens is the ballgame: on BEIR scifact the lexical pool's own bm25 order
  scores .682 nDCG@10 and an unweighted density re-scoring of that same pool
  scores .302.
- ``bm25_score_weight`` 100.0 — the weight on ``_bm25``, FTS5's own bm25 score for
  the hit (peak-normalized over the pool). It is the *magnitude* behind
  ``bm25_weight``'s ordinal: the reciprocal-rank proxy is a near-flat gradient by
  construction (at ``rrf_k`` 60 a 200-deep pool spans 1.00 down to 0.23), so it can
  only nudge, where the score separates a doc carrying the rare discriminating term
  from one carrying three common ones — IDF and length normalization the density
  term does not have. It is worth more paired with ``semantic_weight`` than the two
  are apart, each supplying a magnitude the other arm's rank cannot express.
- ``semantic_weight`` 200.0 — the weight on the vector arm's cosine, spread
  min-max across the pool (see :func:`~.rank.score_features`). Fusion weighs the
  arms' *agreement* by rank; this weighs how near the arm actually judged a hit to
  be, which rank-based fusion discards — RRF at ``rrf_k`` 60 cannot tell a 0.72
  cosine from a 0.55 one. It is the larger of the two magnitude terms because it
  moves recall as well as order, concentrated in the vocab-mismatch shapes the
  lexical arm has no purchase on. The cosine must be *spread* rather than used
  raw: unspread it is mostly a constant offset, and since the content-type
  multiplier scales the whole sum, a flat semantic term amplifies content-type
  preference instead of relevance.
- ``thread_evidence_weight`` 0.0 — off. The signal is how many distinct matches
  the pool holds from a hit's thread, and it fails for a reason that is structural
  rather than measured: evidence favours the thread that returns to a subject over
  the thread that settles it in one exchange, so a broad query whose answer is one
  *specific* conversation loses it, at every weight down to 25. The log damping
  bounds how far a chatty thread can climb, not whether it climbs past a
  single-mention answer. A query-shape gate is the seam that would earn it.
- ``recency_weight`` 1.0 — the corpus skews to OLD threads, so a strong
  recency boost buries what users actually read; 1.0 keeps a mild recent
  tiebreaker. The signal itself decays exponentially with
  ``recency_half_life_hours`` (~3 days) into a 1–20 score.
- ``density_weight`` 100.0 / ``phrase_weight`` 50.0 — term density
  (matched terms per ``density_norm_chars`` of content) is the primary
  lexical signal; contiguous/near phrases add a bounded bonus on top. Only the
  ratios between the four weights matter — the score is scale-invariant, so
  ``density_weight`` is the anchor the others are read against, and
  ``density_norm_chars`` rescales density itself (a second way to spell the same
  knob). ``phrase_weight`` has never been resolved by a measurement: the
  synthetic corpus's contiguous-vs-scattered pair is decided by density alone
  either way. It stays for the proximity shape it protects — a remembered exact
  wording — not on a measured delta.
- ``content_type_weights`` ``None`` means the production table
  (:data:`thread_archive._retrieval.rank._CONTENT_TYPE_WEIGHT`); a mapping
  replaces it wholesale (``{}`` weighs every content type 1.0).
- ``rrf_k`` 60 — the reciprocal-rank-fusion constant for merging the arms.
- ``pool_floor`` 200 — candidate pool depth. Not ``limit*5`` alone because
  reachability dies at the pool boundary: a relevant-but-old hit past bm25's
  top-N is unreachable no matter how the ranker weighs it.
- ``coherence_gamma`` ``None`` defers to the env knob
  (``$THREAD_ARCHIVE_COHERENCE``); a float forces the community-coherence
  re-rank's strength.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class SearchParams:
    """One retrieval configuration. Frozen: an experiment is a value, not a
    mutation — build variants with ``dataclasses.replace``."""

    density_weight: float = 100.0
    phrase_weight: float = 50.0
    recency_weight: float = 1.0
    fusion_weight: float = 400.0
    bm25_weight: float = 100.0
    bm25_score_weight: float = 100.0
    semantic_weight: float = 200.0
    thread_evidence_weight: float = 0.0
    content_type_weights: Optional[Mapping[str, float]] = None
    recency_half_life_hours: float = 72.0
    density_norm_chars: int = 500
    rrf_k: int = 60
    pool_floor: int = 200
    coherence_gamma: Optional[float] = None


#: The shipped configuration — what ``search()`` runs when no params are given.
DEFAULT = SearchParams()
