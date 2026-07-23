"""Re-enable the cross-encoder cheap: pool 8, passage 512 — the budget candidate."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The rebuild candidate: turn the auto-re-rank back on but small enough to "
              "fit the latency budget (pool 8, passage 512 — a fraction of the tokens the "
              "old budget scored). If it recovers most of the rich budget's gold delta at "
              "a fraction of the cost, it's the re-rank worth shipping back on.")
PARAMS = SearchParams(rerank_auto=True, rerank_pool=8, rerank_doc_chars=512)
