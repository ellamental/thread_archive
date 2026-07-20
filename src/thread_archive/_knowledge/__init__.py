"""The knowledge layer: the corpus graph and the data plane under it.

The archive owns the knowledge graph's **data** and its **analytics**:

* **data plane** — every graph mutation is an append-only ``KgEvent`` in the
  truth log, folded by :mod:`.materialize` into the ``thread_links`` /
  ``topic_messages`` projections; :mod:`.read` serves the SQL-only topic reads
  the core needs (search's ``topic_id`` scope, the subjects lens, topic pages'
  link/citation lists) and owns the hierarchy vocabulary.
* **graph analytics** (:mod:`.graph`, :mod:`._community`) — the in-process
  corpus graph over the store: nodes are the curated topics plus every thread
  the curation touches (evidence citations, link endpoints); PageRank,
  communities (Leiden via ``leidenalg`` + ``python-igraph``, networkx Louvain
  as a fail-soft fallback), bridges, peers. Pure projection over archive
  tables — no curation code involved.

What *writes* this data — the librarian/gardener curation agents, their MCP
write surface, and the drains — is the separate ``thread-librarian`` package
(its own repo), which builds on these modules. The graph is empty (all queries
return empty) until topics + links exist; the core archive works uncurated.
"""

from __future__ import annotations

from .graph import (
    community_members_for,
    get_bridge_topics,
    get_communities,
    get_community_peers,
    get_community_topic_ids,
    get_status,
    get_topic_graph_meta,
    get_topic_graph_metadata,
    get_unconnected_topics,
    reset_cache,
)
from .materialize import apply_event
from .read import topic_get, topic_members, topic_thread_ids, topic_tree

__all__ = [
    # graph analytics
    "get_communities",
    "get_topic_graph_metadata",
    "get_topic_graph_meta",
    "get_community_topic_ids",
    "get_community_peers",
    "community_members_for",
    "get_bridge_topics",
    "get_unconnected_topics",
    "get_status",
    "reset_cache",
    # data plane
    "apply_event",
    "topic_get",
    "topic_members",
    "topic_thread_ids",
    "topic_tree",
]
