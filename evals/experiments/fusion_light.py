"""The pre-rebuild fusion weight (100) — the delta the shipped 400 is holding."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("Fusion at 100, where density outweighs cross-arm agreement several "
              "times over. The configuration the paraphrase rebuild replaced: it "
              "leaves the vector arm's vocab-mismatch answers sunk under lexically "
              "dense confounds. Kept as the regression arm — this is the gap the "
              "shipped weight is buying, re-measurable without re-deriving it.")
PARAMS = SearchParams(fusion_weight=100.0)
