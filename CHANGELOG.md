# Changelog

## Unreleased

- `self-update` updates the PyPI distribution: it resolves the newest release, gates the wheel it downloaded on that
  file's declared truth format, installs it, smoke-checks, and rolls back to the running version on failure. A clone
  and a `uv tool`/`pipx` environment each report unavailable, naming the command that does move them.
- Development moves to the `dev` branch; `main` now holds one snapshot commit per release, with the tags clones follow.
- **Search returns every matching message.** Results are no longer grouped by thread: a conversation matching eight
  times gives eight rows, each with its own snippet and context, and `limit` counts messages. The `group=` parameter
  is gone (`thread`/`nested`/`dup`/`none`/`browse`), as is `collapse=` and the CLI's `--group` / `--collapse`. A fold
  to one-row-per-thread bought brevity with the thing a search is for — it dropped every match after the first, and
  answered "which threads mention this" with a smaller number than the truth. What survives is the same-anchor
  dedup, which is deduplication rather than grouping: a title doc and the event it anchors to are one message.
  Costed at ~1.8x rendered tokens for equal conversation coverage on real traffic, 3.4x worst case.
- The viewer's search results and browse rows **paginate**. Both pages of `/search` carry a first/prev/next/last
  walker above and below the rows, the page rides the URL (`?page=`) so a deep page is shareable and survives opening
  a thread and coming back, and the results line says where the page sits in the match set — in the same words the
  MCP renderer uses, `≥` and all, so the two surfaces describe one archive alike. The old "top 40 shown — narrow the
  query" line is gone: it named a cut nothing could walk past.
- A search whose candidate pool saturated now reports the **real** size of the match set (`count_matches`) rather than
  how far the pool reached, still flagged `exhaustive=False`; `pages` divides what a walk can actually reach, so it
  advertises no page that returns nothing. The walk itself is bounded by pool depth.
- The docs read from the packaged install outward: PyPI badges, how a pip install updates, SECURITY.md's update model
  covering both install shapes and PyPI's build attestation, and a provider plugin installed into whatever environment
  archive runs from (venv, `uv tool --with`, `pipx inject`) rather than a clone's `.venv`.
- The agent-driven installers (`claude-install.md`, `claude-install-ubuntu.md`) are gone: `pip install thread-archive`
  plus `thread-archive setup` is the install, and the README's from-source block is the clone path. It absorbs what
  only lived in them — the C toolchain the `[leiden]`/`[embeddings]` wheels can want, and the Python floor on older
  Ubuntu.

## 0.0.8 — 2026-07-28

- PyPI is a supported install (`pip install thread-archive`): a `v*` release tag publishes the wheel + sdist via
  Trusted Publishing. The base dependency pins `mcp<2` — mcp 2.0 removes the FastMCP API archive-mcp imports.
- Search ranks the same across restarts: the corpus graph persists beside the vector pack (TTL-bounded, shape-checked),
  `status` reports it, and a restart re-reads nothing — fingerprints persist and unchanged files skip the parse.
- Ingest is instrumented per stage (`thread-archive source ingest`), checkpoints and MCP serving itemize their time,
  telemetry ledgers rotate to stamped segments readers walk in full, and embed drains return torch's cached memory.
- Search quality is gated at release: `python -m search_lab gate` holds the bench set to accepted numbers, failing
  regressions, unmeasured ranking changes, and stale rows. `thread_search(commit=…)` resolves all files, not 300.
- The stats page gains charts and a real time axis; backup verify bounds mirror coverage on both sides; a disabled
  self-updater stops filing update notices; bug reports collect `status` + `source coverage` output up front.

## 0.0.7 — 2026-07-27

- The command is `thread-archive`; verbs group under `source`/`index`/`backup`/`service`, old spellings still resolve.
- Retrieval reaches the shell (`search`, `read`), `web` opens the viewer, `uninstall` takes it back off a machine.
- Search's p99 falls from 57s to ~1.5s: the cross-encoder is gone and the vector pack rebuilds off the request path.
- Ranking now scores its arms rather than their ranks, and weighs match coverage; BEIR scifact nDCG@10 0.509 → 0.650.
- Search enumerates as well as finds — `page=`, real totals, `path=`/`commit=` scopes; tool output leaves the index.
- Search stops hiding threads: near-identical rows are marked (`_dup_thread_ids`), not folded away — the fold fired
  on 55% of real queries. `collapse=True` restores it. A ranked walk now reaches past its pool via the exact-set
  reconciliation, so `group='browse'` is no longer a separate shape — it is a legacy spelling of the default.
- Gone: redaction and its keyring, the topic graph, and the measurement surface; updates are operator-run only.
- Python floor 3.12; the base install is lexical-only (Leiden behind an extra); a `setup` re-run keeps your opt-outs.
- The viewer opens as a retrieval workspace, takes an account export by drag-and-drop, and routes no dev page.
- Searches, web requests, watcher passes and loads are timed; `status` reports disk cost and degraded capabilities.
- A red backup leaves `backup-failures.jsonl`; Codex stops importing as Claude Code; stats stop double-counting tokens.

## 0.0.6 — 2026-07-21

- The `archive` script is gone; `thread_archive` is the one front door (setup + every verb); reinstall agents to repoint.
- Linux (systemd `--user`) support: launchd gives way to a platform-neutral backend registry; a new OS is now a drop-in.
- The archive no longer knows thread-librarian exists; the subjects lens moves in-tree, curated analytics move out.
- The search stack is tunable end to end via a frozen `SearchParams`; an `evals/` lab races configs on mined queries.
- Ranking gains a corpus-native coherence signal, live when the reranker stands down; embed/rerank models are injectable.
- `thread_archive eval`: a read-only search-quality self-checkup over your own archive; nothing leaves the machine.
- Web viewer: a header line naming a thread's subagents and a Playwright gate over the prod bundle; topic pages retire.
- A first-run test finds every harness's store in its default spot on macOS and Linux; Docker and package lanes gate it.
- Security hardening: config fails closed, a bare MCP is read-only, the viewer sends CSP; SECURITY/CONTRIBUTING land.
- Truth-format boundaries fail closed with migrate-before-restart; the backup mirror now deletes renamed ULID twins.

## 0.0.5 — 2026-07-20

- Thread ids are ULIDs (truth format v2); legacy integer ids resolve forever as aliases; a one-shot migration ships.
- Curation moves out: thread-librarian is its own repo and plugin; archive keeps the knowledge data plane and graph.
- Providers are pluggable: a public provider API (discovery, render policies, session-id shapes) the built-ins share.
- Installs self-update from release tags: daily probe, 48h soak, smoke check, rollback — pushing a tag is shipping.
- Import drift self-repairs: raw source mirror, unmodeled-field preservation, degradation verdicts, `archive fix-import`.
- Retrieval is measured on real usage: the log-mined click eval gates CI; the re-rank pays only on vocab-mismatch heads.
- FTS drops to external-content (~4 GB smaller), the semantic matrix mmaps; search gains browse, grouping, dup folds.
- Agent-run threads leave default search; `thread_read` grows ends/last/topics modes; images and attachments are viewable.
- The knowledge graph projects over the whole corpus (evidence edges, thread nodes), not the librarian's links alone.
- Importer dropped-field sweep over every provider; the amendment seam and backfills enrich already-stored history.

## 0.0.4 — 2026-07-16

- Self-curation runs on a schedule: `archive curate librarian|gardener` spawns bounded headless drains, installed as
  hourly/daily LaunchAgents; the librarian writes stored summaries again and the gardener gets MCP diagnostics.
- `archive restore` does real recovery (staged rebuild, atomic publication, `--generation`); backups sync a
  `.recovery/` bundle (config, keyring, retained exports), enforce owner-only mirrors, and fail on a stale bundle.
- The graph reads back out (`topic_get`/`topic_members`, curated topic pages, `topic_id` search scope); the viewer
  gains topics/hierarchy views, an all-threads page, search-hit deep links, hook rendering, last-activity ordering.
- Retrieval usage is ledgered with latency (ids only); the topical arm and the usage-mined golden eval are deleted.
- Capture health stops over-alarming; export redrops merge grown conversations and originals are never deleted;
  ChatGPT branch structure and claude.ai attachments reach truth; repair dumps are purged from git history.
- Privacy/hardening: 0700 homes, loopback-only MCP, pinned embed revision, 300s bulk write timeout, coverage floors.

## 0.0.3 — 2026-07-15

- Distribution moves to clone-install (the brief PyPI/Homebrew run is retired): `pip install -e .` from a clone or
  `pip install git+<repo-url>@vX.Y.Z`; releases are annotated `vX.Y.Z` tags cut per `docs/releasing.md`.
- `archive redact`: crypto-shredding redaction (reversible / escrowed / forgotten) across every store without breaking
  the truth log's shape, verify, or dedup; setup schedules the nightly backup (`archive daemon install --backup`).
- One shared MCP server (`archive-mcp --http` behind a LaunchAgent) replaces a ~3 GB resident model per stdio client.
- Search ~5× faster at identical results, quality CI-gated (MRR/recall floors) over a usage-mined golden set; the
  topic graph demoted to a `subjects:` lens; assistant text indexed; Codex turns get the real serving model.
- Capture blind-spot detection (skip ledger, watch-pass heartbeat, `archive coverage` reconciliation); ChatGPT exports
  import and zero-yield imports are quarantined, not deleted; SMB destinations work; ingest rides out FTS rebuilds.
- Hardening: viewer loopback guard, suite isolation + `tests/meta/` ratchets, `_truth`/`_ops` split, mypy gate.

## 0.0.2 — 2026-07-11

- First release published to PyPI (since retired — see 0.0.3): first-run setup wizard (discovers stores, imports with
  narration, wires watcher + MCP), a status view, and `config.json` choices every ingest path honors.
- Zero-daemon path: `archive-mcp` cohosts lazy catch-up ingest under an ingest-owner flock; `archive daemon` verbs
  manage the launchd watcher from the package.
- Public API narrowed to the retrieval MCP tools + the versioned truth format (`docs/format.md`); retrieval CLI verbs
  removed, the Python API privatized, all ratchet-pinned; version single-sourced from `__version__`.
- Nightly publishes a tier-aware verdict; verify hardening (fresh-connection quick_check, cross-store parity, named
  failure evidence); a content-hash cursor rewind and write-mutex mirror copies close three silent-loss paths.
- Boundary test lanes: wheel into a clean venv driven over MCP stdio, SIGKILLed-writer durability, provider goldens,
  frontend component tests, per-package coverage floors.

## 0.0.1 — 2026-07-11

- Retrieval correctness pass: the semantic arm sits out scoped/count/oldest searches, exclusions narrow the KNN scope,
  chunk pooling precedes the top-k cut, a bm25 OR-fallback serves conversational queries, `sort='oldest'` is in order.
- Crash-safe truth appends: drain intent journal, all-or-nothing batches, torn-tail repair, exclusive cross-process
  write locking, dedup enforced as a DB constraint.
- Reindex fails closed; backup hardened (atomic publishes, shrink guard, restore drills); `archive nightly` runs
  backup → verify → drill under a monitored heartbeat; `verify` grew tiers; `archive repair` restores red to green.
- One-time duplicate repair: ~27k duplicated turns collapsed, ~237k dedup keys backfilled; rebuilds reproduce the
  repair instead of undoing it.
- Retrieval overhaul: titles/summaries indexed as docs, long-document chunking, identifier-recall fixes, auto-widening
  MCP scope, an MRR/recall eval harness; the suite pinned model-free (~2 min → ~8 s), coverage on every CI sweep.

## 0.0.0 — initial release

- Serverless local archive over `~/.thread/archive`: append-only JSONL truth plus a rebuildable SQLite index.
- Multi-provider importers: Claude Code, Cursor, OpenCode, ChatGPT/Anthropic exports, Grok, cloth, and friends.
- Watcher daemon (`archive watch`) tails harness stores and ingests continuously.
- Retrieval MCP server: `thread_search` (FTS + optional semantic/rerank arms) and `thread_read`.
- Librarian MCP: topics, citations, thread links, and a review queue for knowledge curation.
- Read-only web viewer cohosted at `:8787` (`archive watch --web`).
- CLI: import/export, reindex, checkpoint, backup, verify.
