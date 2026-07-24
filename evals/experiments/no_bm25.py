"""Zero the bm25 term — rank the pool without the lexical arm's own verdict."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = (
    "Without _lex the only lexical signal left is density, which is IDF-blind and "
    "length-normalized — it weighs a common term like the rare one that "
    "discriminates, then divides by length. Zeroing the term should cost the "
    "query-shaped files (findability, judged, rerank-cases) their head order and "
    "hand recall back to the topic-shaped ones, which lean on density. Under "
    "--models the fusion term hides most of the damage, since _rrf carries the "
    "lexical rank too; a lexical-only pool is where it shows."
)
PARAMS = SearchParams(bm25_weight=0.0)
