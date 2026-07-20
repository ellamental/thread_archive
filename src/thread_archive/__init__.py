"""thread-archive: a serverless-native local archive for AI conversations.

JSONL is the durable truth log; SQLite is a rebuildable projection. One storage
path, no server backends.

The public API is exactly two things:

* the retrieval MCP tools — ``thread_search`` and ``thread_read``, served by
  ``archive-mcp``,
* and the on-disk truth format (``docs/format.md``, versioned by
  ``manifest.json``'s ``version``) — the durability promise: data written by
  one release stays readable by the next. Read-only access to the documented
  stores themselves (``index.db`` is plain SQLite, the truth directory is
  documented JSONL) rides on this contract.

**Everything else is private support machinery for those two products** and
may change without notice: the ``archive`` CLI (the process seam launchd,
cron, and operators use), the web viewer, and every
underscore-prefixed module — :mod:`._api`, the coordination layer, included.
There is no public Python API. More surface gets exposed deliberately as it
matures, not by accident of being installed or importable.
``tests/test_public_api.py`` ratchets this boundary.
"""

from __future__ import annotations

# Single source of version truth — pyproject declares `dynamic = ["version"]`
# and hatchling reads it from here at build time.
# Versioning policy: 0.0.x while the public API is the retrieval MCP tools +
# the truth format only; everything else is free to change without notice.
# Don't bump past 0.0.x as part of release mechanics.
__version__ = "0.0.4"

__all__ = ["__version__"]
