"""Weaken length normalization 4× (density per 2000 chars instead of 500)."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("Coarser length normalization lets long low-signal dumps compete with "
              "short focused threads that mention the terms at the same absolute count.")
PARAMS = SearchParams(density_norm_chars=2000)
