"""SearchParams — every tunable of the retrieval pipeline in one object.

The production pipeline (:func:`thread_archive._retrieval.search`) reads its
numeric knobs from a :class:`SearchParams`; :data:`DEFAULT` carries the shipped
values, so ``search(query)`` and ``search(query, params=SearchParams())`` are
the same ranking. A candidate configuration is just another instance —
``SearchParams(recency_weight=0.0)`` — passed through ``search(params=...)``
and scored against the incumbent on identical cases by the search lab
(``scripts/search_lab.py``, ``experiments/``) and the quality-corpus harness
(``tests/quality_corpus.run_cases(params=...)``).

The shipped values, with their evidence:

- ``fusion_weight`` 50.0 — the MRR optimum for the fused lexical+vector
  ranking (the fusion sweep: 50.0 Pareto-dominates 0.0 on R@1/10/20 and MRR;
  swept on the title-proxy eval). Lexical scoring is ~0 for a semantic-only
  hit, so without this term a vocab-mismatch hit the vector arm surfaced
  would sink regardless of its rank.
- ``recency_weight`` 1.0 — the corpus skews to OLD threads, so a strong
  recency boost buries what users actually read; 1.0 keeps a mild recent
  tiebreaker. The signal itself decays exponentially with
  ``recency_half_life_hours`` (~3 days) into a 1–20 score.
- ``density_weight`` 100.0 / ``phrase_weight`` 50.0 — term density
  (matched terms per ``density_norm_chars`` of content) is the primary
  lexical signal; contiguous/near phrases add a bounded bonus on top.
- ``content_type_weights`` ``None`` means the production table
  (:data:`thread_archive._retrieval.rank._CONTENT_TYPE_WEIGHT`); a mapping
  replaces it wholesale (``{}`` weighs every content type 1.0).
- ``rrf_k`` 60 — the reciprocal-rank-fusion constant for merging the arms.
- ``pool_floor`` 200 — candidate pool depth. Not ``limit*5`` alone because
  reachability dies at the pool boundary: a relevant-but-old hit past bm25's
  top-N is unreachable no matter how the ranker weighs it.
- ``rerank_pool`` 24 — how many ranked candidates feed the cross-encoder
  before cutting to ``limit``: wide enough to cover recall@20, small enough
  to keep the in-process re-rank quick.
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
    fusion_weight: float = 50.0
    content_type_weights: Optional[Mapping[str, float]] = None
    recency_half_life_hours: float = 72.0
    density_norm_chars: int = 500
    rrf_k: int = 60
    pool_floor: int = 200
    rerank_pool: int = 24
    coherence_gamma: Optional[float] = None


#: The shipped configuration — what ``search()`` runs when no params are given.
DEFAULT = SearchParams()
