"""The pre-lean cross-encoder budget: pool 24, passage 1500 chars."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The shipped re-rank budget was cut for latency (pool 24->12, passage "
              "1500->768). This is the old rich budget: if it recovers gold-file MRR/nDCG "
              "meaningfully, the cut traded away quality worth paying ~4x the re-rank "
              "cost for; if it doesn't, the lean default is free speed.")
PARAMS = SearchParams(rerank_pool=24, rerank_doc_chars=1500)
