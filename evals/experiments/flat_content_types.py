"""Every content type weighted 1.0 — no user/title favoritism, no tool/thinking discount."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The content-type table matters: weighting all types flat lets noisy "
              "tool output and derived summaries crowd intentional user text out of the head.")
PARAMS = SearchParams(content_type_weights={})
