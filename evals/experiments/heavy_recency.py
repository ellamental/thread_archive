"""Recency weight 10× the shipped value."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("A strong recency boost hurts: the corpus skews old, so recency-heavy "
              "ranking buries dense, focused threads under whatever happened lately.")
PARAMS = SearchParams(recency_weight=10.0)
