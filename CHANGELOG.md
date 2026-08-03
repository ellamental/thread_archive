# Changelog

## Unreleased

## 0.0.15 — 2026-08-03

- Substring search rides a trigram index: infix queries 30–60x faster, exact counts past the examine cap, and a
  bounded per-query probe declining it where the scan wins; `thread-archive index substring` heals older archives.
- Claude Science imports on the app's own clock (`_ts`) and validates under its own source; unmodeled message keys
  are preserved and ledgered. Claude Code's ledger declares `fallback` blocks and two live assistant-line fields.
- The release machinery gets a weekly TestPyPI drill (publish path, rc opt-in, `self-update` end to end), Publish
  `verify` runs a real ingest lifecycle on the published wheel, and PyPI becomes the only supported install path.
- Release seams close: `release_finish.sh` blocks until Publish `verify` is green, Bench pins actions by SHA, the
  install lane pre-pulls its base image, and dataset pins re-read files younger than a two-second settle window.
- The restore drill goes weekly, age-gated — nightly again while red; Bench CI keeps a per-run perf-trend artifact.
- Dead surface shed across import parsers, watcher telemetry, and service management (one API by agent name).

## 0.0.14 — 2026-08-01

- CI proves both platforms: macOS lanes for the pytest bar and the wheel install, beside the Linux matrix now
  carrying 3.12–3.14, the serial suite, a devweb lane, and the Docker from-nothing install proof on release PRs.
- Releasing is machinery, not memory: a release-shape check holds the §4 contract on every PR to main, the main
  ruleset requires every green (CI, Bench, CodeQL, install) before merge, and a merge without a bump fails loudly.
- The off-box quality gate runs pre-merge on every release PR, not only weekly; the portability proof ships green.
- `scripts/audit_release_settings.py` audits the GitHub-side hardening; credentialed workflows pin actions by SHA
  (dependabot keeps pins fresh); `release_cut.sh` / `release_finish.sh` script the cut and the finish.
- `watch --web` survives a ^C during startup: the interrupt gets the same clean stop and close as one in the loop,
  and closing a viewer server whose startup failed no longer crashes on a missing attribute.

## 0.0.13 — 2026-08-01

- The quality gate runs off this box: bench corpora and built homes ship as content-hashed release assets on the
  private thread-archive-bench-data repo (`python -m search_lab packs`; hashes of record in `bench-packs.json`), and a
  scheduled GitHub lane (`bench.yml`) restores them and runs `gate --run --quick` paying only for CPU query embedding.

## 0.0.12 — 2026-07-31

- Releases publish a mandatory rc first: GitHub CI, CodeQL, and the publish path are proven before the final ships.
- Web and devweb static requests resolve via an index of the built bundle; nothing the build didn't emit can be served.
- `match='substring'` honors `OR`/`|` as a union; `since`/`until` reject unknown units; hits carry source and date.
- `thread-archive search` delegates to a live shared MCP server (~0.5s vs ~6s cold); fallback re-runs in-process.
- Import fixes: claude-science incremental slicing, cursor/opencode settled-window re-scans, cursor WAL watched.
- The open archive is an object: per-archive caches under a non-recycling identity; opening no longer writes env vars.
- The KNN matrix never builds inline in a request thread; pack builds serialize on a machine-wide lock, assemble faster.
- The shipped manual is `docs/public/` (default-closed wheel include); stale retrieval rationales audited out.
- New ratchets: retrieval surface derived from the tool signature, provider conformance kit, truth↔index property test.
- The suite runs serially; coverage floors ratchet behavior, not presentation; the CI latency gate is removed.

## 0.0.11 — 2026-07-30

- The bench's quick tier is sized against this box's slow days rather than its median — the same row measures a factor
  of two apart across runs, so a sample fitted to a good day is one busy afternoon from blowing `QUICK_ROW_BUDGET_MIN`.
  PerLTQA's arms sample 1,200 of 8,588 and `cdr[vectors]` 350 of 1,583 (rows `~1200`, `~350`), each landing near three
  minutes under load; baselines re-accepted at those sizes and the published tables read from them.
- Every workflow's `GITHUB_TOKEN` is read-only by default; the publish job alone widens it, for Trusted Publishing.

## 0.0.10 — 2026-07-30

- Retrieval cost: warm passes serialize per machine and load the persisted graph rather than rebuild it (8.9s → 0.12s),
  idle servers hold their pages resident, embeds are memoized, and the exact-set memo survives ingest (852 → 19ms).
- The web viewer is dev-only and ships in no wheel; the dev panels are their own server (`python -m devweb` on :8789);
  the wheel drops eleven one-shot repair scripts; the dev toolchain is a PEP 735 group (`pip install -e . --group dev`).
- The manual ships in the wheel: `thread-archive docs <page>` and the viewer's `/docs` serve the same packaged pages.
- Every benchmark corpus is pinned to a content hash and the upstream revision that reproduces it; the release gates on
  the bench's quick tier (`gate --run --quick`, under 20 minutes); BEAM re-baselined to its scored 279 questions.
- Runtime telemetry records only on a dev install (`"dev_mode": true`); fault records are not telemetry and still write.
- Security pass: ZIP decompression ceilings, the MCP HTTP DNS-rebinding allow-list, `git` pinned off repository config.
- MCP tool descriptions cost ~930 context tokens, not ~3,150 (long form via `thread_help`); `thread_search(pr=…)` lands.

## 0.0.9 — 2026-07-29

- Releases ship by PR: `release/X.Y.Z` stabilizes off `dev` in a worktree, the operator merging to `main` is the
  ship, and `release.yml` turns the merge into the annotated tag and the PyPI publish.
- Search returns every matching message — no thread grouping (`group=`/`collapse=` gone); viewer search and browse
  paginate; a saturated pool reports the real match count; stored thread summaries are no longer indexed.
- Retrieval cost: id-scoped semantic masks build in vector space (~2× faster), an indexing batch yields the embed
  model per chunk so queries wait ~1s not the batch, and a Cursor poll costs what moved (4.6s → 15ms).
- Ingest faults get a durable folded record (`ingest-errors.jsonl`; a `faults:` line in `status`); notices carry
  failure counts; schema mismatches report as themselves; ops split interactive from bulk, cold starts by door.
- `self-update` moves PyPI installs, format-gated with rollback; README is a landing page with reference in `docs/`;
  the agent-driven installers and the `arguana` benchmark are gone.

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
