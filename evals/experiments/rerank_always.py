"""Force the cross-encoder on every query — both gates (query shape, strong
lexical head) bypassed. A code-style experiment: SEARCH wraps the production
pipeline and overrides one argument."""

from thread_archive._retrieval import search as _production

HYPOTHESIS = ("The re-rank gates are protective: forcing the cross-encoder onto "
              "queries the lexical arm already nails shuffles solved heads (measured "
              "0.55->0.48 MRR on the title eval). Only measurable under --models.")


def SEARCH(query, **kw):
    kw["rerank"] = True
    return _production(query, **kw)
