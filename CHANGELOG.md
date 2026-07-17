# Changelog

## Unreleased

- The web viewer gains a **stats** page: token and cost analytics over the whole archive — overview
  totals + activity span, a by-provider table (conversations, tokens, and average session cost for the
  pay-per-token sources that record it), and a by-model breakdown listing every model used. Cost is read
  straight from the `api_request_completed` payloads that carry it (subscription tools log tokens but no
  dollar figure, shown honestly as "—"). Backed by an incrementally-maintained per-(thread, model) rollup
  (`thread_metrics` + a `metrics_cursor` watermark) that folds only new events, so the survey stays fast
  on a multi-GB index; the rollup is a derived projection that rebuilds itself after a reindex.
- Archive-operational (curation drain) threads are created with `exclude_from_search` set, and existing
  ones were backfilled — librarian/gardener transcripts quote search hits wholesale, so they matched
  nearly any query about their own subjects. Subagent `system` threads stay searchable.
- Web viewer search snippets show the matched line plus one line of context on each side and unwrap the
  Grok `<user_query>` wrapper to the real prompt — the result list reads like the thread it opens instead
  of leaking `<user_query>`/`<user_info>` tags. The status bar polls instead of fetching once, so a
  transient blip (a watcher restart cycling the cohosted server) no longer latches a permanent "archive
  unavailable" banner.
- The topic graph is followable from search for read-only consumers: the `subjects:` header carries each
  subject's `[topic <id>]`, a hint line teaches the moves, and the docstrings advertise that `thread_read`
  on a topic id renders its curated page (description, links, cited quotes).
- Stored summaries yield to verbatim evidence in ranking (content-type weight 1.2 → 0.6): a librarian
  digest stays findable but no longer crowds the record it summarizes out of the top ranks.
- Curation drains spawn from `<home>/curation`, and the claude-code importer files any session run from
  inside the archive home as a hidden `system` thread (`archive_operational`) — a drain's own transcript
  no longer re-enters the librarian queue for future drains to summarize.
- Backup drops all disk-durability posturing: the `same_device` flag, the Time Machine probe
  (`external_disk_coverage`), the same-filesystem warnings, and every "different disk/machine" /
  off-machine suggestion in docs and wizard copy. Disk durability is the user's concern, like any
  other data; the backup's scope is recovering from bad writes. "Durability kit" is now the backup kit.

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
