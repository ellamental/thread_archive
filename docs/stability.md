# Stability

The public API is exactly four things:

- **the retrieval tools** — `thread_search` and `thread_read`, served to agents
  by `archive-mcp` and to a person by the `thread-archive search` /
  `thread-archive read` verbs. One implementation, two front doors: their
  parameters, defaults, and output are the same contract either way;
- **the on-disk truth format** — versioned by `manifest.json`'s `version` and
  specified in [format.md](format.md). Data written by one release
  stays readable by the next; a reader refuses a truth directory newer than it
  understands. Programmatic read access to the documented stores (`index.db`
  is plain SQLite; the truth directory is documented JSONL) rides on this
  contract.
- **the provider plugin API** — `thread_archive.provider` and its `parse` /
  `testing` submodules, documented in [providers.md](providers.md).
  A provider maintained outside this repo is written against it and cannot
  follow the private tree's churn, so these names keep working.
- **the web viewer's URLs** — the local UI at `http://127.0.0.1:8787`
  ([web-viewer.md](web-viewer.md)): its page routes and the two JSON endpoints
  other programs call. Editor buttons, sibling navbars, and health probes link
  these from outside the repo, so they keep working.

Everything else is private support machinery and may change without notice:
the rest of the `thread-archive` CLI, the viewer's bundle and markup, and every
other Python module. More surface gets exposed
deliberately as it matures. `tests/test_public_api.py` ratchets the boundary,
with the viewer's page routes pinned in `frontend/e2e/route-coverage.spec.ts`
against the route table itself.

Releases (changelog compression, version bump, release commit, annotated tag)
follow [releasing.md](releasing.md).
