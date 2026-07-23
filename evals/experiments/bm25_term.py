"""Admit the lexical arm's own placement into the score as its own term."""

from thread_archive._retrieval import SearchParams

HYPOTHESIS = (
    "The ranker throws away bm25's verdict and re-scores its pool with an "
    "IDF-blind, length-normalized density term, so weighting the arm's own "
    "placement (_lex) recovers head order the re-scoring loses. Out of domain, "
    "where the fusion term is dead and density is the whole ranker, this is the "
    "difference between .302 and .682 nDCG@10 on BEIR scifact; in domain the "
    "fused stack already reaches most of it through _rrf, so the gold files "
    "should move much less — and the topic files, which lean on density, should "
    "start giving back recall as the weight climbs."
)
PARAMS = SearchParams(bm25_weight=100.0)
