# Contributing

Bug reports and fixes are welcome. Before sinking time into a feature PR,
open an issue first — the scope here is deliberately narrow (see the README's
*Not supported* list; those are decisions, not backlog).

## Providers: plugins first

The most useful contribution surface is provider support, and it usually
doesn't need a PR at all: a provider is one descriptor written against the
public `thread_archive.provider` API and published under the
`thread_archive.providers` entry point — a first-class source (same poll
loop, same coverage reporting) that you maintain on your own release
schedule. See [docs/providers.md](docs/providers.md). PRs that graduate a
proven external provider into the built-ins are welcome too.

## The bar for a change

- **Green suite**: `.venv/bin/pytest tests/ -q` (the package lane,
  `-m package`, if you touched packaging). CI also runs ruff, mypy, the
  per-package coverage floors (`scripts/coverage_gate.py`), and the frontend
  checks — all of it without secrets, so fork PRs run the full gate.
- **Optional integrations stay optional.** Some tests `importorskip` packages
  that aren't part of this repo; a skip on a bare `[dev]` install is expected,
  and the coverage floors hold without them. Don't add a hard dependency to
  make a skipped test run.
- **Respect the stability boundary** (README → Stability): the MCP tools, the
  on-disk truth format, and the provider plugin API are public contracts;
  `tests/test_public_api.py` ratchets the boundary. Everything else is
  private and free to churn.
- **Docs describe the present.** No change-history in docstrings or comments
  — that story belongs in `CHANGELOG.md` (add a line under `## Unreleased`).
- **Frontend changes** rebuild the committed bundle: `cd frontend && npm run
  build`, run `npm test` and `npm run e2e`, and the regenerated `_web/static/`
  assets land in the same PR. If dependencies changed, also regenerate the
  bundled license notices: `python scripts/gen_third_party_notices.py .` A
  first checkout needs `npm install && npx playwright install chromium` in
  `frontend/` before those checks.

macOS is the supported platform; CI's ubuntu runners prove the Python core
imports and passes off-mac, nothing more. Python ≥ 3.14.

Security reports go through [SECURITY.md](SECURITY.md), not the issue
tracker.
