"""thread-archive: a serverless-native local archive for AI conversations.

JSONL is the durable truth log; SQLite is a rebuildable projection. One storage
path, no server backends.

There is deliberately **no public Python API**. The public surface is exactly:

* the ``archive`` CLI (:mod:`.cli`),
* the two MCP servers (``archive-mcp`` / ``archive-librarian-mcp``),
* and the on-disk truth format (``docs/format.md``, versioned by
  ``manifest.json``'s ``version``) — the actual durability promise: data
  written by one release stays readable by the next.

Programmatic access goes through the tools (CLI / MCP) or, read-only, through
the documented stores themselves: ``index.db`` is plain SQLite and the truth
directory is documented JSONL. Every underscore-prefixed module — including
:mod:`._api`, the coordination layer the CLI, MCP servers, and web viewer call —
is private and may change without notice. ``tests/test_public_api.py`` ratchets
this boundary.
"""

from __future__ import annotations

# Single source of version truth — pyproject declares `dynamic = ["version"]`
# and hatchling reads it from here at build time.
# Versioning policy: 0.0.x while the public surface is tools + format only;
# the _api module is free to change without notice. Don't bump past 0.0.x as
# part of release mechanics.
__version__ = "0.0.1"

__all__ = ["__version__"]
