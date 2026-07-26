"""Gold-mining miners for the search-quality bench.

Development machinery, not product: this package is excluded from the wheel, so
``thread_archive mine`` is a repo-only command that runs beside the ``search_lab/``
bench its output feeds (an install answers it with a pointer to the repo).

Each miner mints snapshot-bound eval ``--cases`` rows from a different real
signal; :mod:`._framework` defines the shared contract and the command
``thread_archive mine`` (list / ``<miner>`` / ``all``) drives them. This package
stays import-light: the miner modules (and their sqlalchemy / api imports) load
lazily via :func:`load_registry`, so ``python -m thread_archive._mine tool ...``
— the hot per-search corpus seam the agents shell into — pays for nothing but
:mod:`._corpus`.

The miners share three cores with the bench they feed — the scoring engine, the
snapshot binding, and the run ledgers — and those live in ``search_lab/``, beside
the harnesses that read what mining writes. :func:`_bootstrap_lab` is what makes
them importable: the checkout root goes on ``sys.path``, which is sound here for
the same reason the wheel exclusion is — this package only exists in a checkout.
"""

from __future__ import annotations


def _bootstrap_lab() -> None:
    """Put the checkout root on ``sys.path`` so ``search_lab`` imports resolve.

    Silent when the directory is absent: an install that somehow carries ``_mine``
    fails at the first lab import with a plain ImportError naming what's missing,
    which beats a path hack that pretends to have worked.
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]  # <repo>/src/thread_archive/_mine
    if (root / "search_lab").is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_lab()


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
