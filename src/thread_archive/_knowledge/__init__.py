"""The knowledge layer's data plane — the curated records under the archive.

The archive owns the knowledge graph's **data**, nothing more:

* every graph mutation is an append-only ``KgEvent`` in the truth log, folded
  by :mod:`.materialize` into the ``thread_links`` / ``topic_messages``
  projections;
* :mod:`.read` serves the SQL-only topic reads (one topic's page, its
  citations, the thread set behind search's ``topic_id`` scope, the part-of
  hierarchy) and owns the hierarchy vocabulary.

This is a compatibility surface: existing KG records keep reading and keep
surviving reindex, but the archive neither writes nor analyzes them. What
*writes and analyzes* this data — the librarian/gardener curation agents,
their MCP surface, and the corpus-graph analytics (PageRank, Leiden
communities, bridges, peers, the subjects lens) — is the separate
``thread-librarian`` package, which builds on these modules. The graph is
empty (all queries return empty) until topics + links exist; the core archive
works uncurated.
"""

from __future__ import annotations

from .materialize import apply_event
from .read import topic_get, topic_members, topic_thread_ids, topic_tree

__all__ = [
    "apply_event",
    "topic_get",
    "topic_members",
    "topic_thread_ids",
    "topic_tree",
]
