# Stability

The public API is exactly four things:

- **the retrieval tools** — `thread_search` and `thread_read`, served to agents
  by `archive-mcp` and to a person by the `thread-archive search` /
  `thread-archive read` verbs. One implementation, two front doors: their
  parameters, defaults, and output are the same contract either way. The MCP
  server serves a third tool, `thread_help`, whose name and topics are contract;
  the manual it returns is prose, and prose is never one;
- **the `thread-archive` CLI** — every verb, documented in [cli.md](cli.md).
  It is the process seam: service manifests, cron entries, operator scripts and
  fingers all name these verbs, and a machine already wired to one cannot follow
  a rename. The command tree and each verb's flags are the contract; the
  human-readable text a verb prints is not, except where a flag names a
  machine-readable shape (`search --output linkable`). New verbs and new flags
  are additive and arrive without ceremony; a spelling that ever worked keeps
  resolving (`_LEGACY_VERBS` and `_normalize`), which is what a rename costs
  here.
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
The web viewer is not on this list, because it is not in the product an install
gets: it is dev-only and ships in no wheel ([web-viewer.md](web-viewer.md)).
Within a clone its URLs are still held steady — editor buttons, sibling navbars,
and health probes link `http://127.0.0.1:8787` from outside the repo — and
`frontend/e2e/route-coverage.spec.ts` pins the page routes against the route
table. That is a promise to this machine's own family, not to an installed
package.

Everything else is private support machinery and may change without notice:
the viewer's bundle and markup, the `/api/*` endpoints not named above, and
every Python module in the package — there is no public Python API, and the
`cli` module is public in name only, because it is what the console script
resolves to. More surface gets exposed
deliberately as it matures. `tests/test_public_api.py` ratchets the boundary.

Releases (changelog compression, version bump, release commit, annotated tag)
follow [releasing.md](releasing.md).
