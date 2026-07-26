"""SearchParams — every tunable of the retrieval pipeline in one object.

The production pipeline (:func:`thread_archive._retrieval.search`) reads its
numeric knobs from a :class:`SearchParams`; :data:`DEFAULT` carries the shipped
values, so ``search(query)`` and ``search(query, params=SearchParams())`` are
the same ranking. A candidate configuration is just another instance —
``SearchParams(recency_weight=0.0)`` — passed through ``search(params=...)``
and scored against the incumbent on identical cases by the search lab
(``evals/search_lab.py``, ``evals/experiments/``) and the quality-corpus harness
(``tests/quality_corpus.run_cases(params=...)``).

The shipped values, with their evidence:

- ``fusion_weight`` 400.0 — the cross-backend fusion term (the normalized
  ``_rrf`` agreement score), weighted to compete with density. Density is
  unbounded (matched terms per ``density_norm_chars``), so a short doc carrying a
  few of a long question's common words outscores the fusion term's ceiling
  several times over: a vocab-mismatch answer the vector arm ranks first (high
  ``_rrf``, low density) sinks under lexically dense confounds. Weighting
  cross-arm *agreement* to roughly density's working scale is what keeps it
  reachable — the paraphrase and vague query shapes, where the lexical arm has
  no purchase, are the ones that move. At 400 every gold file but one improves on
  all four metrics, head order included (success@1 rises — the ordering is more
  confident, not flatter); past ~500 the vector arm starts overriding lexical
  evidence it should defer to and the keyword-shaped files give back recall.
  Saturating density instead (``d/(d+k)``, bounding it to compete on fusion's
  scale) buys the same paraphrase recall and costs far more elsewhere: the
  linear term is load-bearing for the topic files.
- ``bm25_weight`` 100.0 — the weight on ``_lex``, the lexical arm's own placement
  of a hit (peak-normalized reciprocal rank; FTS5 orders by bm25 but never
  surfaces the score, so without this term the arm's verdict survives only as
  the order the pool arrives in). It is the counterweight to density's blind
  spot: density is IDF-blind and length-normalized (matched terms per
  ``density_norm_chars``), weighing a corpus-common term exactly like the rare
  one that discriminates and then dividing by length, so a short doc carrying a
  few common query words outranks the long doc carrying the discriminating ones.
  100 is where the query-shaped files gain without the topic-shaped files paying
  much: findability +.019 MRR / +.015 nDCG@10, judged +.013 MRR, rerank-cases
  +.052 success@10, against the trade that buys it — the frustration file gives
  back .048 recall@10 and context-compaction .033. It is a real trade, not a free
  win, and the direction is bounded: 200 buys findability another .015 nDCG@10
  for more of the same recall, and past ~400 bm25's order starts overriding the
  density evidence the topic files lean on and they break their floors.
  The term matters most where ``_rrf`` cannot reach. Fusion runs only when the
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
  term does not have. 100 is the peak: it lifts every floored file's nDCG@10
  (findability +.010, judged +.012, rerank +.010) with no file's recall paying, and
  past ~200 the confound-dense topic files give back recall as bm25's verdict starts
  overriding the density evidence they lean on (400 costs the pooled bench .013
  nDCG@10, 800 costs .043).
- ``semantic_weight`` 200.0 — the weight on the vector arm's cosine, spread
  min-max across the pool (see :func:`~.rank.score_features`). Fusion weighs the
  arms' *agreement* by rank; this weighs how near the arm actually judged a hit to
  be, which rank-based fusion discards — RRF at ``rrf_k`` 60 cannot tell a 0.72
  cosine from a 0.55 one. It is the larger of the two magnitude terms because it
  moves recall as well as order: +.020 recall@10 and +.025 nDCG@10 on the protocol
  files at 200. Past ~400 it starts overriding lexical evidence it should defer to
  and the recall-capable ``judged`` file gives back a case; the raw cosine is worth
  roughly a third of the normalized one, because unspread it is mostly a constant
  offset that the content-type multiplier scales into a content-type preference.
- ``thread_evidence_weight`` 0.0 — off. The signal (how many distinct matches the
  pool holds from a hit's thread) is the largest measured lever on subject-shaped
  queries: +.022 nDCG@10 / +.017 recall@10 over the 22 ``topic`` gold files, where
  the product's standing headroom lives. It ships off because of what it trades for
  that. Evidence favours the thread that returns to a subject over the thread that
  settles it in one exchange, so a broad query whose answer is one *specific*
  conversation loses it: on ``judged`` the query "how can we improve thread_search"
  falls from rank 1 to past 20, and it falls at every weight tested down to 25 — the
  log damping bounds how far a chatty thread can climb, not whether it climbs past a
  single-mention answer. A query-shape gate is the seam that would earn it (the
  subject-shaped queries it helps are the ones ``rank.should_rerank`` already
  classifies); until then the recall-capable file's verdict stands.
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
  knob). The gold files do not resolve ``phrase_weight``: zeroing it moves the
  mean objective by less than one case's worth on the file that drives the
  difference, and the synthetic corpus's contiguous-vs-scattered pair is decided
  by density alone either way. It stays for the proximity shape it protects —
  a remembered exact wording — not on a measured delta.
- ``content_type_weights`` ``None`` means the production table
  (:data:`thread_archive._retrieval.rank._CONTENT_TYPE_WEIGHT`); a mapping
  replaces it wholesale (``{}`` weighs every content type 1.0).
- ``rrf_k`` 60 — the reciprocal-rank-fusion constant for merging the arms.
- ``pool_floor`` 200 — candidate pool depth. Not ``limit*5`` alone because
  reachability dies at the pool boundary: a relevant-but-old hit past bm25's
  top-N is unreachable no matter how the ranker weighs it.
- ``rerank_auto`` ``False`` — whether a query auto-invokes the cross-encoder
  re-rank on the conceptual-shape gate. Off by default: the cross-encoder is the
  pipeline's dominant latency (measured 2–4s on a long conceptual query, with
  wide variance) and buys ~no gold-file MRR over the fused
  lexical+semantic+coherence stack, so the shipped search stays inside the latency
  budget without it. An explicit ``rerank=True`` still forces it (evals, and the
  quality-rebuild seam that must re-earn it within budget — a smaller model, a
  tighter pool); the community-coherence re-rank still orders the head.
- ``rerank_pool`` 12 — how many ranked candidates feed the cross-encoder
  before cutting to ``limit`` (the head is ``max(rerank_pool, limit)``, so a
  wider result window still reranks its whole depth). The cross-encoder is the
  pipeline's dominant latency; the pool is the first knob on it, kept just past
  the default result window rather than deep into backfill the ranker already
  orders well.
- ``rerank_doc_chars`` 768 — the per-passage character cap the cross-encoder
  scores each hit at (MaxP: a long doc becomes its match-window/head/tail
  passages, each capped here). The dominant knob on re-rank latency alongside
  ``rerank_pool``: cost is per-token, so a pool of ``rerank_pool`` long hits
  scores up to 3× that many passages of this length. The match-window keeps the
  query-centred span; the search lab scores the quality cost of the shorter cap.
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
    rerank_auto: bool = False
    rerank_pool: int = 12
    rerank_doc_chars: int = 768
    coherence_gamma: Optional[float] = None


#: The shipped configuration — what ``search()`` runs when no params are given.
DEFAULT = SearchParams()
