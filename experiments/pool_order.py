"""No weighted ranker at all: every score term zeroed, so the stable sort
returns the candidate pool's own order (bm25, RRF-fused when the vector arm
runs)."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The weighted ranker beats raw pool order: bm25 alone under-ranks "
              "focused threads against long dumps and knows nothing of recency or phrase shape.")
PARAMS = SearchParams(density_weight=0.0, phrase_weight=0.0, recency_weight=0.0,
                      fusion_weight=0.0, content_type_weights={})
