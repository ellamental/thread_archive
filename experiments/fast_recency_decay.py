"""Recency decay six times faster (half-life ~12h instead of ~3 days)."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("A 12-hour decay makes the recency signal a same-day-only tiebreaker; "
              "anything older than yesterday scores the cold floor.")
PARAMS = SearchParams(recency_half_life_hours=12.0)
