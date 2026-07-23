"""Re-enable the cross-encoder at the old rich budget: pool 24, passage 1500."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The cross-encoder auto-re-rank ships OFF (it was the pipeline's dominant "
              "latency for ~no gold gain). This turns it back on at the old rich budget: "
              "the gold-file MRR/nDCG delta over the shipped no-rerank baseline is what "
              "the re-rank would buy back — the quality-rebuild target to beat within budget.")
PARAMS = SearchParams(rerank_auto=True, rerank_pool=24, rerank_doc_chars=1500)
