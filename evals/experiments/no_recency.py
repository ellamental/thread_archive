"""Drop the recency term entirely."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("Recency is a genuine tiebreaker, not noise: without it, near-equal-density "
              "threads stop resolving toward the one you touched last.")
PARAMS = SearchParams(recency_weight=0.0)
