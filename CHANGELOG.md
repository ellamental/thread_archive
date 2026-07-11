# Changelog

## Unreleased

- The vendored provider-parser island moved from a public top-level
  `thread_import` package to `thread_archive._thread_import` — a pip install
  no longer plants a second, generically named public package in
  site-packages, and the parser API stays private until it's deliberately
  exposed.

- PyPI release readiness: version single-sourced from `thread_archive.__version__`
  (pyproject declares it dynamic); `[project.urls]` added;
  sdist contents pinned via `[tool.hatch.build.targets.sdist]` `only-include`
  (hatchling only reads the root `.gitignore`, so `frontend/node_modules` —
  ignored only by the nested `frontend/.gitignore` — was ballooning the sdist
  to 17 MB; it and repo-local dirs like `host/` and `.claude/` are now
  excluded). README gains a `pip install thread-archive` path and the real
  clone URL. Built distributions land in `dist/` (gitignored) for
  `twine upload`.

- Integrity hardening (from the self-review in thread 3716422), four gates at
  the transaction/content seams: (1) the backup mirror traversal now runs under
  the truth-write mutex, so a copy can never capture a mid-drain partial batch
  or a pre-rollback append that the shrink guard would then pin in the mirror;
  (2) `verify --hashes --backup` now *fails* on a mirror hash-mismatch count
  above the previous run's for that destination (it was report-only), with the
  same fails-once baseline absorption as the live scan (`backup_hashes`
  component, `backup_hashes_last` in health); (3) `rebuild_truth_from_store`
  grew two content pre-flights behind the existing containment gate — it
  refuses when any store payload fails the content hash in its own dedup_key
  (a corrupted index row that kept its id/key must not replace the good truth
  line) and when truth records carry fields the running code's models don't map
  (an older binary must not lossily re-emit newer truth); `force=True` remains
  the deliberate override; (4) `repair_truth` self-validates restore candidates
  the same way — a failing payload is still restored (it's the only copy left)
  but counted (`restored_hash_mismatches`) and logged, and the next
  `verify --hashes` reports it. Plus a fifth, report-only seam: reindex counts
  and logs same-id event content it is about to overwrite in the index
  (`content_overwrites` + sample ids in its result, salvage or not). A blocking
  content gate was rejected (below), but for an *unkeyed* event the index row
  can be the last good copy of a truth line rotted in place, and once the swap
  lands the two stores agree — cross-store parity can never see it again; the
  publication report is the last observable moment of the overwrite.
  Reviewed but deliberately not adopted: a durable per-drain transaction ledger
  (truth-ahead-of-index is the designed safe direction; dedup collapses
  resurrections), a *blocking* content-equality reindex gate (would invert
  truth's authority over the index; cross-store parity in `verify --hashes` is
  the detector, and the report-only counter above covers the laundering
  window), and sticky-red-until-acknowledged hash semantics (the fails-once
  baseline + failure ledger is the documented tradeoff).

- Ingest hardening (from the self-review in thread 3716420): the line-stream
  cursor no longer assumes its source is append-only. The watermark carries a
  sha256 of the bytes it was computed over (`import_state.last_content_hash`), and
  each poll re-hashes the file's prefix to prove the imported lines are still the
  file's first lines — a mismatch rewinds to line 0 and re-imports, dedup_key
  collapsing what's already held. This closes three silent-loss paths that all
  looked like "nothing changed" to the old size-equality check: a line rewritten
  to the same serialized length, a truncate-and-regrow between polls, and — the
  sharp one — a malformed *interior* line later repaired, which shifted every
  later line's index out from under a cursor that counts parsed, not physical,
  lines. A torn final line still reads as the append it is, not a rewrite.
- A turn split across polls stays one turn: the assistant reply whose user line
  landed in an earlier poll now inherits that turn's `stream_id` instead of
  opening its own.
- Per-item failures inside the Cursor / OpenCode / Claude Science DB scans reach
  the watcher's health instead of only the log — a caught-and-logged failure left
  the scan looking like a clean "nothing new" while a conversation was missing.
- The import loop asks the index only about the dedup keys it built this pass,
  rather than loading every key in the thread — a poll of a long session no longer
  pays for the whole session's history.
- `init_db` ALTERs in columns added after a table shipped (`create_all` only ever
  issues `CREATE TABLE`, so a live index never grew one). Missing *indexes* stay
  with verify/reindex.
- A red verify names its cause and keeps its evidence: results carry
  `failed_components`, the full result of any failing run is appended to
  `<home>/verify-failures.jsonl`, and the deep/hashes health records now carry
  their own tier's verdict (prompted by a nightly that went red with nothing
  persisted to say why).
- `verify --hashes` grew cross-store payload parity: truth and index payloads
  are fingerprint-compared per id, so rot in an *unkeyed* payload (most
  pre-dedup-key history) is detectable instead of being silently promoted over
  the good index row by the next reindex. Deep verify does the same for the kg
  log's content.
- The curatorial log joined the daily tier: shallow verify parse-scans
  `kg_events.jsonl` and count-checks it against the table (it previously waited
  up to a week for the deep pass), and the deep kg id-diff is now
  watermark-bounded so a live librarian write can't false-alarm it.
- `verify --backup` fails on a mirror whose effective count dropped since the
  previous scan (the drill's coverage floor only sees drops ≥2%).
- Watcher poll errors surface in health.json / `archive status` instead of
  living only in stderr logs.

## 0.0.1 — 2026-07-11

- Retrieval correctness pass (from the self-review in thread 3716414): the
  semantic arm now sits out of `tool_name`-scoped, count, and oldest searches
  (tool docs aren't embedded, so every fused hit violated the filter);
  `exclude_content_type` narrows the KNN scope itself instead of discarding
  candidates post-top-k; vector chunk max-pooling moved *before* the top-k cut
  so one long doc can't eat candidate slots; natural-language queries get a
  bm25 OR-fallback tier over meaningful terms when the strict all-terms MATCH
  under-fills (fixes hard zero-recall on conversational queries, lexical-only
  installs especially); `sort='oldest'` scans the index chronologically so the
  pool holds true first mentions; the vector matrix cache is canonicalized by
  scope and bounded.
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
