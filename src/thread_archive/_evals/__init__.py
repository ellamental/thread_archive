"""Quality-guard harnesses run against an archive (private, like all `_` modules).

Home for evaluation machinery that measures or guards retrieval quality over a
real archive: :mod:`.incidents` is the reality-integrity incident harness
(recorded search failures replayed as permanent guards). The retrieval metric
harness (`scripts/retrieval_eval.py`) is an operator script today; if it
matures into package code, it lands here too.
"""
