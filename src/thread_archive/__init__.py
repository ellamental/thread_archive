"""thread-archive: a serverless-native local archive for AI conversations.

JSONL is the durable truth log; SQLite is a rebuildable projection. One storage
path, no server backends.

The public surface is exactly:

* the names in ``__all__`` below (re-exported from :mod:`.api`),
* the ``archive`` CLI (:mod:`.cli`),
* the two MCP servers (``archive-mcp`` / ``archive-librarian-mcp``),
* the manifest hook (``python -m thread_archive.manifest``),
* and the on-disk truth format (``docs/format.md``, versioned by
  ``manifest.json``'s ``version``).

Every underscore-prefixed module is private and may change without notice.
``tests/test_public_api.py`` ratchets this boundary.
"""

from __future__ import annotations

from .api import (
    backup,
    bridge_topics,
    checkpoint,
    close,
    embed,
    import_path,
    knowledge_status,
    nightly,
    open_archive,
    read_thread,
    read_thread_structured,
    reindex,
    repair,
    restore_drill,
    search,
    status,
    topic_peers,
    verify,
    watch,
)

# Single source of version truth — pyproject declares `dynamic = ["version"]`
# and hatchling reads it from here at build time.
# Versioning policy: 0.0.x until there is a stable, deliberately exposed public
# API; the api.py surface is still free to change without notice. Don't bump
# past 0.0.x as part of release mechanics.
__version__ = "0.0.1"

__all__ = [
    "__version__",
    "open_archive",
    "close",
    "search",
    "read_thread",
    "read_thread_structured",
    "import_path",
    "reindex",
    "embed",
    "checkpoint",
    "watch",
    "status",
    "knowledge_status",
    "bridge_topics",
    "topic_peers",
    "backup",
    "verify",
    "repair",
    "restore_drill",
    "nightly",
]
