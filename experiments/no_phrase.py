"""Drop the phrase-proximity bonus entirely."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = ("The phrase-proximity bonus earns its keep: without it, contiguous-phrase "
              "queries lose ground to threads that merely scatter the same words.")
PARAMS = SearchParams(phrase_weight=0.0)
