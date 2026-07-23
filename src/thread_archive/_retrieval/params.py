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
- ``bm25_weight`` 0.0 — the weight on ``_lex``, the lexical arm's own placement
  of a hit (peak-normalized reciprocal rank; FTS5 orders by bm25 but never
  surfaces the score). Off by default because the fused stack already reaches
  that verdict through ``_rrf``, which fuses the lexical and vector *ranks* —
  so on the gold files, all of which run the fused pipeline, admitting it a
  second time is a trade rather than a clear win: at 100 the findability file
  gains .015 nDCG@10 and rerank-cases .052 success@10, while the frustration
  file gives back .048 recall@10; at 200 findability gains .030 nDCG@10 and
  context-compaction and frustration pay for it in recall; past ~400 the topic
  files break their floors outright as bm25's order overrides the density
  evidence they lean on. The term exists for the case ``_rrf`` cannot cover.
  Fusion runs only when the vector arm returns, so a **lexical-only** search — a
  ``tool_name`` or ``types`` scope, a structural query, an archive with no
  embeddings — carries no rank evidence at all and ranks on density alone.
  Density is IDF-blind and length-normalized (matched terms per
  ``density_norm_chars``), which weighs a common term like the rare one that
  discriminates and then favours the shorter doc. Out of domain, where that path
  is the whole stack, the cost is the ballgame: on BEIR scifact the lexical
  pool's own bm25 order scores .682 nDCG@10, and the density re-scoring of that
  same pool scores .302.
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
    bm25_weight: float = 0.0
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
