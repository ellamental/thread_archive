# Changelog

## Unreleased

- **ChatGPT account exports import (2026-07-11).** The vendored ChatGPT parser
  is now wired into the bulk export importer: `classify_export` tells the two
  `conversations.json` providers apart (sibling files first — ChatGPT ships
  `chat.html`/`user.json`, claude.ai `users.json` — then the conversation shape
  itself: `chat_messages` vs `mapping`), and `import_chatgpt_export` lands
  threads as `source='chatgpt'`. Previously a ChatGPT ZIP dropped into
  `dumps/` was misclassified as claude.ai, imported **zero** conversations,
  and was then deleted as a "successful" import — the setup wizard was
  advertising a drop the pipeline destroyed. The drop watcher also gained a
  guard for the general shape of that failure: a recognized export whose
  import processes zero conversations is quarantined to `dumps/failed/`, never
  deleted.

- **The durability kit moved to `_ops/` (2026-07-11).** `_api.py` had grown to
  ~2,200 lines, three-quarters of it backup/verify/nightly implementation; the
  implementations now live in `_ops/backup.py`, `_ops/verify.py`,
  `_ops/nightly.py`, and `_ops/health.py`, with `_api` re-exporting the public
  entry points so the coordination surface is unchanged. No behavior change.

- **Python type gate (2026-07-11).** A `mypy` row joined ci.toml (config in
  pyproject `[tool.mypy]`), matching the type bar the frontend already had via
  `tsc`. ~40 mechanical annotation fixes landed with it; modules that predate
  the gate (`_thread_import.*`, `_scripts.*`, `_truth.jsonl_log`,
  `_ops.verify`) are excluded via per-module overrides — a ratchet list to
  shrink, not policy.

- **Dead vendored code removed (2026-07-11).** `_thread_import/exporters/`
  (CursorExporter + kv/parse mixins, ~1,000 lines) had no consumers and no
  tests; deleted.

- **Hygiene (2026-07-11).** `coverage.json` untracked (it was committed *and*
  gitignored, so every CI sweep dirtied the tree); a stale-name sweep after the
  `web/`→`_web/` rename and the `archive web` verb removal (README, pyproject
  and ci.toml comments, frontend package.json/vite.config/api.ts, host README);
  the landed `docs/plans/reindex-atomic-swap.md` plan deleted (its rationale
  lives in the `jsonl_log` docstrings); `save_config` now fsyncs before its
  rename like every other durable write; CLI verb→api dispatch tests added.

- **Homebrew tap published (2026-07-11):**
  `brew install ellamental/thread-archive/thread-archive` (or `brew tap
  ellamental/thread-archive` then `brew install thread-archive`). The tap
  (github.com/ellamental/homebrew-thread-archive) carries a virtualenv
  formula over the PyPI sdist, dependencies as prebuilt wheels installed
  hermetically into the keg. The `[embeddings]` extra stays pip-only.

## 0.0.2 — 2026-07-11

First release published to PyPI: `pip install thread-archive`.

- **`thread_archive` — first-run setup and the human status view (2026-07-11).**
  The consumer front door the pip story was missing: `pip install
  thread-archive` then `thread_archive` runs an interactive setup that
  *discovers* the machine's conversation stores (stat-only dry run: counts,
  sizes, date ranges — new `SourceWatcher.discover()`), shows what it found
  and where copies will live before touching anything, imports with per-source
  narration (importer log noise routed to `<home>/logs/setup.log`), then
  offers the launchd watcher and MCP wiring (`claude mcp add` run for you at
  user scope, or the JSON block printed for any other client). Every step is
  skippable; choices persist in the new `<home>/config.json`, and disabled
  sources are honored by every ingest path (daemon, lazy MCP catch-up,
  `archive watch`) via `enabled_watchers()`. Re-running lands on a status
  view; `thread_archive setup` re-enters the flow; `--yes` is the
  non-interactive twin (a non-TTY run without it only prints guidance — and
  checking "is this set up?" no longer scaffolds an empty home as a side
  effect). Both `thread_archive` and `thread-archive` console scripts ship.
  `archive` stays the operator seam.

- **Verify's index self-check no longer false-alarms "malformed inverted index"
  (2026-07-11).** Both of today's nightly `verify` failures — the
  highest-severity alarm the system has, fired twice on a healthy archive —
  came from running `PRAGMA quick_check` on a pooled connection: FTS5's
  integrity check consults per-connection segment-structure state, and a
  connection that lives across the watcher's continuous `event_search` rewrites
  can report a healthy index as malformed (reproduced 3-of-5 on a warm pool
  while a simultaneous fresh connection said `ok` every time; likely an
  upstream SQLite 3.51 bug in the FTS5 xIntegrity path). The pragma now runs on
  a private, just-opened connection to the engine's own database file
  (`_api.verify`). Corruption that surfaces as a *raised* `DatabaseError`
  (rather than result rows) is now also captured as a red `quick_check`
  verdict instead of crashing verify; locked/can't-open stays an error so
  environmental trouble can't impersonate corruption. A new test corrupts an
  index page on disk and requires verify to name `quick_check` red.

- **The package lane now drives the installed `archive-mcp` binary the way a
  consumer's client does (2026-07-11):** JSON-RPC over stdio against the
  clean-venv install — tools/list, the first `thread_search` on a virgin home
  (the regression shape of the first-open schema race below), and search+read
  over imported data. The lane's CLI lifecycle test also caught up with the
  retrieval-verb removal (it still called `archive search`/`read`; it now
  exercises ingest + the durability kit, with retrieval covered via MCP).

- **First search on a virgin archive no longer dies with "table already exists"
  (2026-07-11).** `init_db`'s `create_all` existence check isn't atomic with its
  CREATEs, so the MCP server's warm-models search (a background thread at
  startup) racing the first tool call on an empty home made one of them lose the
  CREATE and error — breaking the very first `thread_search` of a fresh install.
  `init_db` now serializes openers in-process and retries a lost cross-process
  race (`_store/schema.py`).

- **`archive-mcp` cohosts lazy catch-up ingest, and the watcher LaunchAgent
  installs from the package (2026-07-11).** The zero-daemon install path:
  `pip install thread-archive` + `claude mcp add thread-archive -- archive-mcp`
  is now the whole setup — the MCP server runs a throttled background ingest
  pass at startup and around tool calls (`_watcher/lazy.py`;
  `THREAD_ARCHIVE_MCP_INGEST=0` disables), so search sees current
  conversations with nothing else installed. Cross-process safety is a new
  ingest-owner flock (`<home>/.ingest-owner.lock`): a lazy pass runs only
  while holding it exclusively, and the watcher daemon holds it for its whole
  lifetime — so with the daemon alive (or several MCP servers racing), exactly
  one process ingests and the rest skip, losing nothing (sources replay from
  import state; dedup_key collapses any overlap).
  The always-fresh upgrade is now `archive daemon install|uninstall|restart|
  status` (`_launchd.py`, macOS-only): the plist is generated in-package,
  pointing at the installed `archive` console script — no repo checkout, no
  sed. `host/Makefile` delegates its watcher targets to the verb (its template
  plist is deleted; the Makefile's remaining value-add is the thread-family
  manifest and the backup agent). Mac-only is a deliberate product decision.

- **The public API is narrowed to exactly two things (2026-07-11): the
  retrieval MCP tools (`thread_search` / `thread_read`, served by
  `archive-mcp`) and the on-disk truth format (docs/format.md).** Everything
  else is now declared private support machinery — the `archive` CLI, the
  librarian MCP server, the web viewer, and all Python modules. Not ready ≠
  not shipped: the private pieces keep running (launchd, cron, and the /ci
  skill drive the CLI), they just carry no external stability promise; more
  surface gets exposed deliberately as it matures.
  As part of the same narrowing, `archive search`, `archive read`, and
  `archive web` are removed (with `_web.serve`, the foreground server only
  `web` called): retrieval traffic belongs to the public MCP tools, and the
  viewer's persistent URL was already the watcher's (`archive watch --web`),
  so the standalone verbs were extra doors onto the same surface. The CLI
  keeps only ingest (`import`, `import-export`, `watch`, `embed`) and the
  durability kit (`backup`, `verify`, `restore-drill`, `reindex`, `repair`,
  `status`, `nightly`). `tests/test_public_api.py` pins the verb set — as
  internal-wiring coordination (plists, cron, skills reference these verbs),
  not as public API — and pins the *absence* of retrieval verbs.

- **A proven-fixed stage retires its own failure (2026-07-11).** The only thing
  that could clear a failed nightly was another full nightly (~1h, restore-drill
  dominated), so an archive that had been fixed *and re-verified at full strength*
  went on announcing itself unprotected until 04:00 came around. `nightly` now
  publishes a **verdict** rather than a transcript: `_pipeline_verdict` takes the
  last nightly's `failed_stages` and subtracts every stage a later, at-least-as-
  strong run has since proven good, and `_stamp_heartbeat` republishes it — called
  from `verify` / `backup` / `restore_drill` as well as `nightly`, so re-running a
  stage on its own clears the board.
  The strength comparison is the guard, and it matters for verify alone: the
  nightly escalates verify on age gates (`deep`, `hashes`, and the mirror
  parse-scan, which rides `--backup` exactly when deep is due), so a later *basic*
  verify passing says nothing about a deep tier that failed. `verify_last` now
  records its tier (`deep` / `hashes` / `backup`) for that comparison to read;
  without it, a cheap green would launder an expensive red. A rerun weaker than
  the check that failed retires nothing — by design.
  The heartbeat gained `nightly_at` (when the pipeline last *ran*) alongside `at`
  (when the file was last written), because those stopped being the same fact the
  moment a lone stage rerun could rewrite it: thread-monitor anchors its
  "backups have stopped happening" staleness check on `nightly_at`, so a hand-run
  verify can no longer mask a dead 04:00 job.

- The test suite grew the boundaries a review flagged as untested — the places
  where reality differs from the in-process synthetic environment:
  - A **package lane** (`tests/test_package_artifact.py`, `-m package`,
    deselected from the default run) builds the wheel + sdist, asserts their
    contents (vendored parsers, provider JSON schemas, pre-built web assets, no
    stray top-level packages), installs the wheel into a clean venv, and runs
    the real `archive` CLI lifecycle from it. The Docker install test now
    builds and installs the wheel instead of `pip install -e`.
  - A **multiprocess durability suite** (`tests/test_multiprocess_durability.py`)
    exercises the flock/fsync/drain-intent protocol with real processes:
    writers SIGKILLed at each drain window (via `tests/mp_child.py`), four
    concurrent importers, and a same-session import race — each converging to a
    verified, single-copy store.
  - **Provider goldens** (`tests/test_provider_goldens.py` +
    `tests/goldens/providers/`): every importer's full normalized truth output
    is locked against a reviewed golden file (regenerate with
    `UPDATE_GOLDENS=1`), so an importer change shows as a reviewable diff.
  - **Migration/repair script tests** (`tests/test_migration_scripts.py`) cover
    the run()/apply/backup/idempotence paths of the `_scripts` migrations,
    including `repair_grok_tool_names` (previously 0% — its committed plan file
    doubles as the fixture).
  - **Frontend component tests** (Vitest + Testing Library, `npm test` /
    the `frontend-test` CI row): block rendering, loading/error/empty states,
    search grouping, uuid→thread redirects, model grouping. Previously the SPA
    only had a typecheck.
  - **Coverage became a gate**: the CI pytest row measures branch coverage and
    `scripts/coverage_gate.py` (its own CI row) enforces per-package floors — a
    regression ratchet calibrated just under measured coverage, instead of one
    global threshold that dormant vendored parser code would render meaningless.
  - Pytest markers with `--strict-markers`: `integration` (real sockets /
    subprocesses; deselectable for hermetic sandboxes) and `package`.

- The family-manifest writer left the package: `thread_archive/manifest.py` is
  now `host/write-manifest.py`. It was the one member of the public surface that
  made no sense to a `pip install` consumer — a thread-family integration point,
  and one that resolved its MCP command paths from a repo checkout (meaningless
  under `site-packages`, where it silently emitted a manifest with no `mcp`
  block). `host/` is installer machinery and is never packaged, which is exactly
  what this is; `make install-agent` and `make manifest` (add `WEB=0` for
  `--no-web`) call it there. `cli` is now the only public module name.

- The public Python API is gone — deliberately. `api.py` became `_api.py`, a
  private coordination layer the CLI, MCP servers, and web viewer call into;
  `thread_archive.__all__` shrank to `__version__`. Nothing outside the repo
  imported the Python surface, and the durability promise was always the
  on-disk truth format, not function signatures. The supported surface is now
  exactly: the `archive` CLI, the two MCP servers, and the truth format
  (docs/format.md). Programmatic read access goes through the
  documented stores (index.db is plain SQLite; truth is documented JSONL).
  The ratchet test now pins the surface at empty.

- The public surface is now locked down to what's deliberately advertised:
  every internal subpackage is underscore-private (`_store`, `_truth`,
  `_retrieval`, `_knowledge`, `_importers`, `_watcher`, `_mcp`, `_web`,
  `_scripts`, `_config`), leaving only `api`, `cli`, and `manifest` at public
  names. `api.embed` (the `archive embed` backend) joined `__all__` — it was
  the one public-named api function not re-exported. A ratchet test
  (`tests/test_public_api.py`) pins `__all__`, the api-module surface, and the
  set of public module names, so widening the API is an edit to a pinned list,
  never a naming accident. README gained a Stability section stating the
  supported surface.

- The truth storage format is now versioned and specified: `docs/format.md`
  documents the truth directory (manifest, per-thread files, sharding, record
  shapes, dedup-key form, overlays, kg log, import cursors), the manifest's
  existing `version: 1` is declared as the format version with a bump policy
  (only for changes an existing reader would misinterpret), and readers now
  *refuse* a truth directory declaring a newer version (`TruthFormatError`)
  instead of guessing at unknown layout semantics.

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
