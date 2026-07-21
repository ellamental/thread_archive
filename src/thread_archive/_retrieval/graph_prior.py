"""Graph-authority ranking prior — the knowledge graph as a retrieval signal.

The librarian's corpus graph (curated links + citation evidence) carries a
usage-independent authority signal; the hypothesis was that a conversation
the curated layer keeps citing is likelier to be the one an agent wants back.
This module turns that into a *bounded, boost-only* prior for the weighted
ranker: per candidate thread, normalized PageRank in ``[0, weight]``, applied
multiplicatively as ``score * (1 + prior)``.

Design constraints, in order:

- **Boost-only.** Most conversations have no curated contact and are not graph
  nodes at all; absence means "no signal", never a penalty. A missing thread
  gets prior 0.0 — its score is untouched.
- **Fail-soft.** The graph stack belongs to the optional ``thread-librarian``
  sibling (dependency tier 3): no librarian, no curation, or any error →
  ``{}`` and the ranker behaves exactly as before.
- **Bounded.** PageRank is normalized by the candidate pool's own maximum, so
  the boost tops out at ``1 + weight`` regardless of corpus shape. The weight
  is deliberately tiebreaker-scale: the prior refines the relevance order, it
  must never outvote the query.

**Off by default — the log-mined eval scores it negative.** On the
click-labeled protocol (250 mined search→read cases, full production stack)
the prior degrades ranking monotonically with weight: MRR 0.250 with the
prior off, 0.247 at weight 0.1, 0.241 at 0.25, 0.240 at 0.5; recall@1 falls
0.168 → 0.152. Authority is query-independent — it floats hub threads over
the specific thread the query names, which is exactly what click labels
punish. The graph signal that *does* pay on this protocol is the
corpus-native community-coherence re-rank (:mod:`.embed_graph`) — pool-
conditioned rather than global. This machinery stays as the opt-in for
re-testing authority variants; the boost is opt-in:
``THREAD_ARCHIVE_GRAPH_RANK=<float>`` sets the weight for a process, anything
else (unset, ``off``, ``0``) keeps it off.

Latency: the first call in a process builds the librarian's cached graph
projection (PageRank + communities over curation-touched threads only —
thousands of nodes, not the corpus); subsequent calls are dict lookups. The
projection is cached per engine and does not track curation writes made after
it builds — an authority prior tolerates that staleness.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_ENV = "THREAD_ARCHIVE_GRAPH_RANK"


def prior_weight(env: str | None = None) -> float:
    """The configured boost ceiling: 0.0 unless a positive float weight is set
    (off by default — see the module docstring for the eval evidence). ``env``
    overrides the environment lookup (tests inject; production reads the
    process env)."""
    raw = (os.environ.get(_ENV, "") if env is None else env).strip().lower()
    if raw in ("", "off", "false", "no"):
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a weight; graph prior stays off", _ENV, raw)
        return 0.0
    return max(0.0, val)


def normalize_pagerank(pagerank: dict[str, float], weight: float) -> dict[str, float]:
    """Scale raw PageRank values into boost addends in ``[0, weight]``,
    normalized by the pool's own maximum. Pure — the testable core."""
    if weight <= 0.0 or not pagerank:
        return {}
    peak = max(pagerank.values())
    if peak <= 0.0:
        return {}
    return {tid: weight * (v / peak) for tid, v in pagerank.items() if v > 0.0}


def thread_graph_prior(thread_ids: list[str], *, weight: float | None = None) -> dict[str, float]:
    """Boost addends for ``thread_ids`` from the librarian's corpus graph, or
    ``{}`` (disabled / librarian absent / no curation / any failure). Values
    feed :func:`thread_archive._retrieval.rank.rank_search_results` as
    ``thread_prior``."""
    w = prior_weight() if weight is None else weight
    if w <= 0.0 or not thread_ids:
        return {}
    try:
        from thread_librarian import graph as _graph
    except Exception:  # noqa: BLE001 — sibling product, fail-soft only (tier 3)
        return {}
    try:
        meta = _graph.get_topic_graph_metadata(thread_ids)
        pagerank = {tid: float(m.get("pagerank") or 0.0) for tid, m in meta.items()}
        return normalize_pagerank(pagerank, w)
    except Exception:  # noqa: BLE001 — a ranking refinement must never break search
        logger.exception("graph prior failed; search continues without it")
        return {}
