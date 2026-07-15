# Changelog

## Unreleased

- **Usage-mined golden retrieval eval removed (2026-07-15).** The 599-case golden
  set (search→read pairs mined by `golden_from_usage.py`), the miner, its tests,
  and `retrieval_eval.py --golden` are gone: the mined pairs assumed a
  click-on-a-result usage pattern that isn't how the archive's results actually
  get used, so the numbers weren't meaningful. The CI retrieval gate is unchanged —
  it runs the `--auto-titles` protocol, which never used the golden set.

- **The format-drift alarm detects drift again (2026-07-15).** Every recent
  validation-drift record (315/315 over the prior week) was the claude-code parser
  flagging its own deliberate preservation block types (`unknown_line`, `attachment`,
  `model_change`) — self-noise that buried any real signal. Those types are now
  registered as expected, and the real signal got sharper: an `unknown_line` block
  whose *line kind* isn't one the parser knowingly preserves (`last-prompt`,
  `ai-title`, `custom-title`, `mode`, `file-history-delta`) files a finding naming
  the new line type specifically.

- **`archive status` shows the nightly pipeline's verdict (2026-07-15).** A red
  nightly (e.g. the offsite stage failing on a TCC denial) was invisible in status
  while an ad-hoc same-disk mirror showed "backup: ok" — the one line that says
  whether the archive survives disk loss lived only in health.json. Status now
  prints `nightly: ok/FAILED (stages) → dest`. The same-filesystem backup warning
  is also rethought: offsite is opt-in and the archive can't assume it knows the
  machine's whole posture, so the shouty WARNING is now a factual note — and when
  the disk is covered by a detectable external backup (Time Machine, via `tmutil`),
  the note says so instead of implying the archive is unprotected.

- **Coverage warns when an account-export source goes stale (2026-07-15).** claude.ai
  and ChatGPT reach the archive only via manual exports; nothing nudged when the
  last drop aged out (chatgpt sat 127 days stale, silently). `archive coverage` now
  warns (never red) when an export-fed source's newest event exceeds 45 days;
  disabling the source in config.json silences it.

- **Store write timeout raised 60s → 300s (2026-07-15).** Bulk maintenance
  (full-corpus FTS rebuilds, repair/backfill migrations) holds the SQLite write
  lock for several minutes; 60s was undersized for exactly that case and errored
  35 watcher imports into health during the 07-12 migration window.

- **Reindex survives an archive that mixes full and event-only thread files
  (2026-07-15).** `reindex` bulk-loads each batch of thread records with one
  `INSERT OR REPLACE` executemany, which SQLAlchemy compiles from the first row's
  keys and binds every row against. A synthesized minimal thread stub
  (`{id, name}` — standing in for a thread file whose `type:thread` metadata line
  was lost to a crash between an import commit and its checkpoint) carries a
  different key-set than a full record, so a batch holding both raised
  `StatementError` and aborted the whole rebuild. Rows are now grouped by key-set
  so each executemany is homogeneous — which also covers records written under a
  since-changed schema landing next to current ones.

- **The watcher daemon converges to ingest ownership even after a lost startup race
  (2026-07-15).** The daemon takes the ingest-owner lock so lazy catch-up passes degrade
  to no-op probes while it's alive. It used to try exactly once at startup and, if a
  transient holder (a lazy pass mid-flight) had it, run lockless for its whole life —
  breaking that guarantee and letting lazy passes run concurrently. It now re-attempts
  the lock each poll until it holds it, then keeps it for its lifetime. (Fixes a flaky
  `test_daemon_run_holds_owner_lock_for_its_lifetime`, which raced the daemon for the
  lock at startup.)

- **Test coverage raised past 90% (2026-07-15).** Branch coverage of the package
  rose from ~81% to 96%, and the per-package `coverage_gate.py` floors were lifted
  to match (every package now floored at ≥90%). New suites cover the CLI verb→api
  dispatch surface, the setup wizard + LaunchAgent lifecycle, the semantic-search
  layer (vectors/embed/rerank, with the models faked so no torch loads), the
  grok/codex/cursor/exports/opencode importers and the vendored
  claude-code/chatgpt/claude export parsers, the watcher poll loop, the
  backfill/recover migration scripts, and the durability-kit edge branches
  (verify/redact/backup/rebuild/drain/repair).

- **Account exports are never deleted, and import loses less (2026-07-15).** The drop
  watcher used to delete an export whenever it processed ≥1 conversation — even when
  some conversations errored, and even when normalization silently dropped
  attachments/images/branch structure the parser didn't carry. A dropped export now
  moves into `dumps/imported/<kind>/` on a fully clean import — kept as the recovery
  copy, bounded to the most recent per kind (a full re-export supersedes the last, so
  the pile can't grow) — and into `dumps/failed/` when any conversation errored
  (preserved as stubs, but the export needs a look). The original download is the last
  copy of anything normalization drops, so it survives regardless. Alongside,
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
