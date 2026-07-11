# Changelog

## 0.0.1 — 2026-07-11

- Crash-safe truth appends: drain intent journal, all-or-nothing batches,
  torn-tail repair, exclusive cross-process truth-write locking, and dedup
  enforced as a DB constraint (`(thread_id, dedup_key)` unique).
- Reindex fails closed: committed-content regression gates, `quick_check` +
  foreign-key checks before the index swap, citations repointed across dedup
  collapse, and vectors survive a plain reindex.
- `verify` grew tiers: shallow daily parity, `--deep` truth↔index diffs and
  search-surface checks, `--hashes` content self-validation with baselines,
  `--backup` mirror scans, and declared-schema parity.
- Backup hardened: atomic publishes, shrink guard, bounded deletions, per-run
  hardlink generations, and restore drills; `archive nightly` runs
  backup → verify → drill with per-stage health records and a heartbeat stamp
  for monitoring.
- `archive repair`: quarantines unparseable truth lines and restores committed
  content — the sanctioned path from a red verify back to green.
- One-time duplicate repair: ~27k duplicated turns collapsed, ~237k dedup keys
  backfilled, and rebuilds now reproduce the repair instead of undoing it.
- Retrieval overhaul: thread titles/summaries indexed as searchable docs,
  long documents chunked into the vector space, canonical time-bound format,
  identifier-recall fixes, auto-widening MCP search scope, and an eval harness
  (MRR / recall@k).
- Test suite pinned model-free (~2 min → ~8 s), integrity tests renamed by
  behavior, coverage measured on every CI sweep, librarian MCP covered.
- `GET /api/archive-link` resolves a candidate list of ids, first real one
  wins.

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
