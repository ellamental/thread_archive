"""The knowledge layer's data plane — the curated records under the archive.

The archive owns the knowledge graph's **data**, nothing more:

* every graph mutation is an append-only ``KgEvent`` in the truth log, folded
  by :mod:`.materialize` into the ``thread_links`` / ``topic_messages``
  projections;
* :mod:`.read` serves the SQL-only topic reads (one topic's page, its
  citations, the thread set behind search's ``topic_id`` scope, the part-of
  hierarchy) and owns the hierarchy vocabulary.

The archive neither writes nor analyzes these records: whatever external
curator exists writes through the truth log (appending ``KgEvent`` rows the
same way :mod:`.materialize` folds them), and any graph analytics live with
that curator. The graph is empty (all queries return empty) until topics +
links exist; the core archive works uncurated.
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
