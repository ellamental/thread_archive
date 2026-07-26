"""Gold-mining miners for the search-quality bench.

Development machinery, not product: this package is excluded from the wheel, so
``thread_archive mine`` is a repo-only command that runs beside the ``evals/``
bench its output feeds (an install answers it with a pointer to the repo).

Each miner mints snapshot-bound eval ``--cases`` rows from a different real
signal; :mod:`._framework` defines the shared contract and the command
``thread_archive mine`` (list / ``<miner>`` / ``all``) drives them. This package
stays import-light: the miner modules (and their sqlalchemy / api imports) load
lazily via :func:`load_registry`, so ``python -m thread_archive._mine tool ...``
— the hot per-search corpus seam the agents shell into — pays for nothing but
:mod:`._corpus`.
"""

from __future__ import annotations


def load_registry() -> list:
    """Every registered miner, in list-view order. Imports the miner modules on
    demand (not at package import) so the corpus tool seam stays cheap."""
    from . import commit_linked, query_mined, querygen, rerank_judged, topic_mined

    return [query_mined.MINER, topic_mined.MINER,
            rerank_judged.MINER, querygen.MINER, commit_linked.MINER]


def dispatch(argv: list[str]) -> int:
    """Run the ``thread_archive mine`` command line (see :mod:`._cli`)."""
    from ._cli import dispatch as _dispatch

    return _dispatch(argv)
