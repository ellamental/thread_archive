"""Gold-mining miners — the deep tier of the search-quality ladder.

The only tokens-spending instrument in the lab: each miner drives headless
``claude`` agents against a frozen corpus snapshot and writes graded relevance
labels the cheaper protocols cannot produce. What it writes is the ``--cases``
files ``retrieval_eval.py`` and the gold gate score, so it lives beside them —
mining is measurement, and none of it reaches a user's install.

Each miner mints snapshot-bound cases from a different real signal;
:mod:`._framework` defines the shared contract and :mod:`._cli` drives them
(``python -m search_lab.mine`` — list / ``<miner>`` / ``all``). This package stays
import-light: the miner modules (and their sqlalchemy / api imports) load lazily
via :func:`load_registry`, so the ``mine tool ...`` corpus seam the agents shell
into on every search pays for nothing but :mod:`._corpus`.
"""

from __future__ import annotations


def load_registry() -> list:
    """Every registered miner, in list-view order. Imports the miner modules on
    demand (not at package import) so the corpus tool seam stays cheap."""
    from . import commit_linked, query_mined, querygen, rerank_judged, topic_mined

    return [query_mined.MINER, topic_mined.MINER,
            rerank_judged.MINER, querygen.MINER, commit_linked.MINER]


def dispatch(argv: list[str]) -> int:
    """Run the mining command line (see :mod:`._cli`)."""
    from ._cli import dispatch as _dispatch

    return _dispatch(argv)
