# Changelog

## Unreleased

## 0.0.4 — 2026-07-16

- Self-curation has a scheduled runner: `archive curate librarian|gardener` gates on work left, then spawns a bounded
  headless drain against the archive's own MCP servers — installable as hourly/daily LaunchAgents (`archive daemon
  install --librarian/--gardener`, offered by the setup wizard; model/effort/cadence live in `config.json`). The
  librarian writes stored summaries again (queue done = cited AND summarized, live threads held back an hour), and the
  gardener gets a diagnostics surface on the librarian MCP: `garden_status` / `garden_queue` / `communities`.
- Recovery is a real verb: `archive restore <mirror> --to <home>` — preflight, staged rebuild, smoke checks, atomic
  publication, `--generation` selection; a replaced home is set aside, never deleted. Backups sync a `.recovery/`
  bundle (config, keyring, retained exports — head-only, so `redact --forget` still crypto-erases), enforce owner-only
  modes on mirrors, and fail the run on a stale bundle; the offsite remount machinery moves to the host operator.
- The knowledge graph reads back out: `topic_get` / `topic_members` tools, `thread_read` renders curated topic pages,
  `thread_search` scopes to a `topic_id`; the web viewer gains `/topics` (communities + hierarchy tree), topic pages,
  an all-threads page with type filters, real link anchors, search-hit deep links, hook/attachment rendering, read
  provenance + message permalinks, per-page search filters, and last-activity ordering in the sidebar.
- Retrieval usage is ledgered (`retrieval-usage.jsonl` — params, hits, reads, latency; ids only, never content); the
  no-lift topical ranking arm and the usage-mined golden eval are deleted; mechanism goldens pin real failure shapes.
- Capture health stops crying wolf: stale watch errors clear on the first clean pass, only substantive skips warn,
  drift detection is de-noised (expected line kinds declared, `cloth_meta` included), and export-fed staleness is aged
  per channel; export redrops merge grown conversations and originals are never deleted, with ChatGPT image/branch
  structure and claude.ai attachments now reaching truth.
- Privacy: tracked repair dumps purged from git history (repo rewritten and recreated), home and truth dirs self-heal
  to 0700, the MCP server refuses non-loopback binds, and the embedding model loads a pinned upstream revision.
- Hardening: store write timeout 60s → 300s for bulk maintenance, reindex tolerates mixed thread-record shapes, the
  watcher converges to ingest ownership after a lost startup race; branch coverage rises past 90% behind per-package
  floors, and private-internal test patches are paid down to zero behind a shrink-only ratchet.

## 0.0.3 — 2026-07-15

Distribution moves to clone-install (the brief PyPI/Homebrew run is retired): `pip install -e .` from a clone, or `pip
install git+<repo-url>@vX.Y.Z`. Releases are annotated `vX.Y.Z` tags cut per `docs/releasing.md`.

- `archive redact`: crypto-shredding redaction (reversible / escrowed / forgotten) that removes content from every store
  without breaking the truth log's shape, verify, or dedup.
- Setup schedules the nightly backup (`com.thread-archive.backup` LaunchAgent; `archive daemon install --backup`).
- One shared MCP server: `archive-mcp --http` behind `com.thread-archive.mcp` replaces a ~3 GB resident model per stdio
  client; standalone stdio lazy-loads.
- Search ~5× faster at identical results; retrieval quality CI-gated (MRR/recall floors) over a 599-case usage-mined
  golden set; the topic graph is demoted to a `subjects:` lens (topical RRF arm off by default — no replicable lift).
- Live-capture assistant text indexed and readable (402 threads backfilled); Codex turns attributed to the real serving
  model (931 repaired in place).
- Capture blind-spot detection: skip ledger, watch-pass heartbeat, `archive coverage` nightly reconciliation; ChatGPT
  account exports import, and zero-yield imports are quarantined instead of deleted.
- SMB backup destinations work; TCC denials are named in nightly errors; ingest rides out FTS rebuilds (60s
  busy_timeout) and reindex swaps; one session-id resolver serves MCP reader and viewer.
- Hardening and reshaping: viewer generic 500s + loopback bind guard, suite isolation + `tests/meta/` ratchets, parser
  damage behavior pinned, truth module split into `_truth/`, durability kit to `_ops/`, mypy gate.

## 0.0.2 — 2026-07-11

First release published to PyPI (since retired — see 0.0.3).

- `thread_archive`: first-run setup wizard (discovers stores, imports with narration, wires watcher + MCP) and a status
  view; choices persist in `config.json` and every ingest path honors them.
- Zero-daemon path: `archive-mcp` cohosts lazy catch-up ingest under an ingest-owner flock; `archive daemon
  install|uninstall|restart|status` manages the launchd watcher from the package.
- Public API narrowed to exactly two things — the retrieval MCP tools and the versioned on-disk truth format
  (`docs/format.md`; readers refuse a newer version) — retrieval CLI verbs removed, the Python API privatized, all
  ratchet-pinned.
- Nightly publishes a verdict: a proven-fixed stage retires its own failure, tier-aware so a cheap green can't launder
  an expensive red.
- Verify hardening: quick_check on a fresh connection (kills a false "malformed index" alarm), cross-store payload
  parity, the kg log in the daily tier, failure evidence persisted with named causes.
- Integrity/ingest hardening: mirror copies under the truth-write mutex, rebuild/repair content pre-flights, a content-
  hash cursor rewind closing three silent-loss paths, per-item DB-scan failures surfaced to health.
- Boundary test lanes: package lane (wheel into a clean venv, driven over MCP stdio), multiprocess durability with
  SIGKILLed writers, provider goldens, frontend component tests, per-package coverage floors.
- Packaging: version single-sourced from `__version__`, sdist contents pinned, vendored parsers privatized under
  `_thread_import`, the family-manifest writer moved to `host/`.

## 0.0.1 — 2026-07-11

- Retrieval correctness pass: the semantic arm sits out scoped/count/oldest searches, exclusions narrow the KNN scope,
  chunk pooling precedes the top-k cut, a bm25 OR-fallback tier serves conversational queries, `sort='oldest'` scans
  chronologically.
- Crash-safe truth appends: drain intent journal, all-or-nothing batches, torn-tail repair, exclusive cross-process
  write locking, dedup enforced as a DB constraint.
- Reindex fails closed (regression gates + integrity checks before the swap); backup hardened (atomic publishes, shrink
  guard, restore drills); `archive nightly` runs backup → verify → drill with a monitored heartbeat.
- `verify` grew tiers (shallow daily parity, `--deep`, `--hashes`, `--backup`, schema parity); `archive repair`
  quarantines unparseable lines and restores committed content — the sanctioned path from red back to green.
- One-time duplicate repair: ~27k duplicated turns collapsed, ~237k dedup keys backfilled, rebuilds reproduce the repair
  instead of undoing it.
- Retrieval overhaul: titles/summaries indexed as searchable docs, long-document chunking, identifier-recall fixes,
  auto-widening MCP search scope, an eval harness (MRR / recall@k).
- Test suite pinned model-free (~2 min → ~8 s); coverage measured on every CI sweep.

## 0.0.0 — initial release

- Serverless local archive over `~/.thread/archive`: append-only JSONL truth
  plus a rebuildable SQLite index.
- Multi-provider importers: Claude Code, Cursor, OpenCode, ChatGPT/Anthropic
  exports, Grok, cloth, and friends.
- Watcher daemon (`archive watch`) tails harness stores and ingests
  continuously.
- Retrieval MCP server: `thread_search` (FTS + optional semantic/rerank arms)
  and `thread_read`.
- Librarian MCP: topics, citations, thread links, and a review queue for
  knowledge curation.
- Read-only web viewer cohosted at `:8787` (`archive watch --web`).
- CLI: import/export, reindex, checkpoint, backup, verify.
