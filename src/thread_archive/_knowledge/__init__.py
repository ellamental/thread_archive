"""Storage mechanics for the truth format's extension region (docs/format.md).

An external knowledge layer keeps a topic graph over this archive. Its records
live in the truth directory so they are backed up, verified and restored with
everything else — and that storage is the whole of archive's involvement. This
package does not create topics, does not curate them, and ships no product
surface over them: no retrieval-tool parameter reaches the graph, and an
archive without it is complete.

What lives here is therefore only what storage requires:

* :mod:`.materialize` folds an appended ``KgEvent`` into the ``thread_links`` /
  ``topic_messages`` projections, so a truth-only restore rebuilds them;
* :mod:`.read` serves SQL reads back out (a node's page and citations, the
  member thread set, the part-of hierarchy) for the writing layer to render.

The writing layer owns the schema, the vocabulary and the compatibility of
these records; none of it is covered by the truth format's version contract.
Every query here returns empty until that layer exists.
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
