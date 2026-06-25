"""Optional knowledge layer: the event-sourced topic graph over ``thread_links``.

Two halves over a pure-graph spine (networkx, no Neo4j, no graph server):

* **read / analytics** (:mod:`.graph`) — the in-process topic graph: PageRank,
  communities (Leiden when the ``[graph]`` extra is present, networkx Louvain as a
  fail-soft fallback), bridges, peers.
* **write / curation** (:mod:`.write`, :mod:`.materialize`) — the librarian's hands:
  create topics, link threads, cite evidence. Every mutation is an append-only
  ``KgEvent`` folded into the projection, so the operation history survives.

The graph is empty (all queries return empty) until topics + links exist — the core
archive works without this layer.
"""

from __future__ import annotations

from .graph import (
    get_bridge_topics,
    get_community_peers,
    get_community_topic_ids,
    get_status,
    get_topic_graph_meta,
    get_topic_graph_metadata,
    get_unconnected_topics,
    reset_cache,
)
from .materialize import apply_event
from .write import (
    add_topic_evidence,
    archive_topic,
    archive_topic_evidence,
    create_topic,
    link_threads,
    merge_topics,
    rename_topic,
    review_queue,
    set_thread_summary,
    thread_user_messages,
    topic_search,
    unlink_threads,
)

__all__ = [
    # read / analytics
    "get_topic_graph_metadata",
    "get_topic_graph_meta",
    "get_community_topic_ids",
    "get_community_peers",
    "get_bridge_topics",
    "get_unconnected_topics",
    "get_status",
    "reset_cache",
    # write / curation
    "apply_event",
    "create_topic",
    "rename_topic",
    "archive_topic",
    "merge_topics",
    "link_threads",
    "unlink_threads",
    "add_topic_evidence",
    "archive_topic_evidence",
    "set_thread_summary",
    # curation-read
    "review_queue",
    "topic_search",
    "thread_user_messages",
]
