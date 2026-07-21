"""Term density alone — phrase, recency, and fusion all zeroed."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("Density is the load-bearing signal: alone it keeps most cases solved, "
              "and the gap to baseline is exactly what the auxiliary signals buy.")
PARAMS = SearchParams(phrase_weight=0.0, recency_weight=0.0, fusion_weight=0.0)
