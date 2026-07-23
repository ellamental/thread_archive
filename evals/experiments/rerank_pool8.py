"""Cut the cross-encoder pool to the result window: rerank_pool 24 -> 8."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("A pool barely past the top-N result window is enough: the re-rank "
              "only needs to float the true target into view. 8 is ~a third of the "
              "cost of 24 — the aggressive end of the pool sweep, to find where "
              "recall starts to break.")
PARAMS = SearchParams(rerank_pool=8)
