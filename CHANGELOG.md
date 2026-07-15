# Changelog

## Unreleased

- **Account exports are never deleted, and import loses less (2026-07-15).** The drop
  watcher used to delete an export whenever it processed ≥1 conversation — even when
  some conversations errored, and even when normalization silently dropped
  attachments/images/branch structure the parser didn't carry. A dropped export now
  moves into `dumps/imported/` on a fully clean import (retained, never deleted — the
  operator prunes it once satisfied) and into `dumps/failed/` when any conversation
  errored (preserved as stubs, but the export needs a look). The original download is
  the last copy of anything normalization drops, so it survives regardless. Alongside,
  three import-fidelity gaps are closed: ChatGPT `image_asset_pointer` parts are
  preserved (an image-only turn kept its pointer in truth instead of vanishing whole);
  ChatGPT's conversation *tree* — each event's `branch.parent_id` and an
  `active_path: false` marker on off-active-path (regenerated) replies — reaches truth
  so branches are reconstructable; and claude.ai `attachments` (with their extracted
  text) + `files` uploads are kept as `content_block` events instead of being ignored.
  New end-to-end goldens (`tests/goldens/providers/chatgpt-web.json`, `claude-web.json`)
  lock all three.

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
