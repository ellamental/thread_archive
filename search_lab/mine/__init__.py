"""Gold-mining miners — the deep tier of the search-quality ladder.

The only tokens-spending instrument in the lab: a miner drives headless
``claude`` agents against a frozen corpus snapshot and writes graded relevance
labels the cheaper protocols cannot produce. What it writes is the ``--cases``
files ``retrieval_eval.py`` scores, so it lives beside them — mining is
measurement, and none of it reaches a user's install.

**Nothing gates on what these produce, and nothing should.** No number mined here
credits a ranking change or blocks a commit; a mined case file is material for a
deliberate, hand-read experiment, and the ``retrieval_free`` split below is what
says how far even that can be pushed.

**A miner declares what fixes its labels, and the strong rung is "not
retrieval".** That is the admission rule, and it is what the registry is for: a
label established by searching with the engine under test can only ever describe
what the incumbent already reaches, so a systematic blind spot stays invisible to
labeler and ranker alike and can never score as a miss. Every miner sets
:attr:`~._framework.Miner.gold_source` naming the artifact its answers come from,
and :attr:`~._framework.Miner.retrieval_free` saying whether retrieval touched
them at all:

- ``retrieval_free=True`` — membership is decided by a record outside the search
  stack: a commit, an edit in the tool-use trail. The gold is what it is however
  the ranker behaves. Their cost is that the *query* must then be authored from
  an artifact, which buys label independence at the price of query realism — a
  query nobody asked is not evidence about queries anybody asks, so passing this
  rung is necessary for a claim and nowhere near sufficient.
- ``retrieval_free=False`` — retrieval helped assemble the pool that was judged.
  Legitimate when the pool unions *several* independent systems plus a random
  draw, which bounds the bias at "what no pooled system finds" rather than "what
  the incumbent misses" — a bound that shrinks as systems are added. This is what
  buys observed queries, since a real query's answer set exists in no record.

Neither rung is the right one for every question, and the split is declared
rather than argued so a reader always knows which kind of number they are
holding.

:mod:`._framework` defines the shared contract and :mod:`._cli` drives the
registry (``python -m search_lab.mine`` — list / ``<miner>`` / ``all``). This
package stays import-light: the miner modules (and their sqlalchemy / api
imports) load lazily via :func:`load_registry`, so the ``mine tool ...`` corpus
seam the agents shell into on every search pays for nothing but :mod:`._corpus`.
"""

from __future__ import annotations


def load_registry() -> list:
    """Every registered miner, in list-view order. Imports the miner modules on
    demand (not at package import) so the corpus tool seam stays cheap."""
    from . import commit_linked, edited_paths, pooled_judged

    return [commit_linked.MINER, edited_paths.MINER, pooled_judged.MINER]


def dispatch(argv: list[str]) -> int:
    """Run the mining command line (see :mod:`._cli`)."""
    from ._cli import dispatch as _dispatch

    return _dispatch(argv)
