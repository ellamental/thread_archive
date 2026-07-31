"""thread-archive: a serverless-native local archive for AI conversations.

JSONL is the durable truth log; SQLite is a rebuildable projection. One storage
path, no server backends.

The public API is exactly five things:

* the retrieval tools — ``thread_search`` and ``thread_read``, served to agents
  by ``archive-mcp`` and to a person by the ``thread-archive search`` /
  ``thread-archive read`` verbs (one implementation behind both),
* the ``thread-archive`` CLI (``docs/cli.md``) — every verb, and the flags each
  takes. It is the process seam: service manifests, cron entries and operator
  scripts name these verbs, so a spelling that ever worked keeps resolving.
  What a verb *prints* is not the contract, only what it is called and what it
  accepts,
* the on-disk truth format (``docs/format.md``, versioned by
  ``manifest.json``'s ``version``) — the durability promise: data written by
  one release stays readable by the next. Read-only access to the documented
  stores themselves (``index.db`` is plain SQLite, the truth directory is
  documented JSONL) rides on this contract,
* the provider plugin API — ``thread_archive.provider`` and its ``parse``
  / ``testing`` submodules (``docs/providers.md``), the surface a provider
  maintained outside this repo is written against,
* and the web viewer — the read-only UI the watcher cohosts at
  ``http://127.0.0.1:8787``: its page routes (``/``, ``/search``,
  ``/threads``, ``/stats``, ``/health``, ``/archive/<thread_id>``) and the two
  JSON endpoints other programs call directly, ``/api/health`` and
  ``/api/archive-link``. Editors, navbars and health probes link these from
  outside the repo, so the URLs keep working. The remaining ``/api/*``
  endpoints back the viewer's own bundle and are private.

**Everything else is private support machinery for those products** and
may change without notice: the viewer's bundle and markup, and every
underscore-prefixed module — :mod:`._api`, the coordination layer, included.
There is no public Python API: :mod:`.cli` carries a public name because the
console script resolves to it, not because its Python names are callable from
outside. More surface gets exposed deliberately as it
matures, not by accident of being installed or importable.
``tests/test_public_api.py`` ratchets this boundary.
"""

from __future__ import annotations

# Single source of version truth — pyproject declares `dynamic = ["version"]`
# and hatchling reads it from here at build time.
# Versioning policy: 0.0.x while the public API is the retrieval tools + the
# CLI + the truth format + the provider plugin API + the web viewer's URLs
# only; everything else is free to change without notice.
# Don't bump past 0.0.x as part of release mechanics.
__version__ = "0.0.11"

__all__ = ["__version__"]
