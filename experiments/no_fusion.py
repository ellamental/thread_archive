"""Zero the cross-backend fusion term (the vector arm's _rrf agreement score)."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The fusion term is what lets a semantic-only hit survive the lexical "
              "ranker; zeroing it sinks vocab-mismatch hits. Only measurable under "
              "--models — the lexical stack has no _rrf to weigh.")
PARAMS = SearchParams(fusion_weight=0.0)
