# Changelog

## Unreleased

- **Shared MCP server: one HTTP daemon instead of a model per client (2026-07-14).**
  `archive-mcp` loads a ~3 GB retrieval stack, and stdio MCP spawns one server per
  connecting client — N agents meant N resident copies (24 live copies ≈ 70 GB of
  footprint, mostly swap/compressed). `archive-mcp --http --host H --port P` now
  serves streamable-HTTP (stateless, JSON responses) so every client shares one
  always-on server. `archive daemon install --mcp` installs it as
  `com.thread-archive.mcp` (default `127.0.0.1:8788`), and clients point their
  `thread-archive` MCP entry at `http://127.0.0.1:8788/mcp` (`type: "http"`) instead
  of the stdio command. Stdio stays the default when no daemon is installed, so
  `claude mcp add … archive-mcp` keeps working standalone. `_launchd.py` now manages
  both the watcher and MCP agents through shared install/uninstall/restart/status
  helpers. Only the shared HTTP server warms the model at startup; a per-client stdio
  server stays lean (~80 MB, model lazy-loaded on first search) rather than each
  holding ~3 GB — so a client that only reads, or whose config was snapshotted to
  stdio before the switch, costs nothing until it actually searches
  (`THREAD_ARCHIVE_MCP_WARM=1` restores eager warming for a standalone stdio box).

- **`_truth.jsonl_log` split into focused submodules (2026-07-14).** The 2,400-line
  truth module now lives as `layout` (paths/manifest/sharding/serialization),
  `locks` (the three flock families), `drain` (append handles + staged writes +
  crash framing), `maintenance` (checkpoint + rebalance), and `rebuild` (scan /
  reindex / re-emit), with `jsonl_log` kept as the facade every consumer imports
  through — no caller changed. The mypy ratchet override carried over to the four
  modules that inherited the shielded code; `locks` and the facade are typed.
  From a Claude self-review of the product (full review in the conversation log).

- **Truth-drain listeners scoped to the archive's own sessions (2026-07-14).** The
  before-commit drain and its compensation listeners registered on SQLAlchemy's
  global `Session` class, firing (as no-ops) for every session in a host process.
  They now register on `ArchiveSession`, the class `get_session` constructs, so
  the archive-as-library imposes nothing on foreign SQLAlchemy sessions.

- **One DB-scan result contract + one poll loop per watcher shape (2026-07-14).**
  The Cursor/OpenCode/Claude-Science scanners now all return
  `_importers.DbScanResult` (`processed`/`imported`/`events_created`/`failed`/
  `errors`), replacing three per-provider dataclasses and the watch loop's
  duck-typed field sniffing. `CoworkWatcher` rides `FileSessionWatcher`'s poll
  loop and `ClaudeScienceWatcher` rides a multi-DB `_DbScanWatcher`, collapsing
  two hand-copies of the fingerprint/retry/prune discipline.

- **Repair undo dumps no longer ship in the wheel (2026-07-14).** The one-shot
  `_scripts` had accumulated ~450 KB of `*_backup_*` / `*_plan_*` operator dumps
  inside the package tree, and hatch packages everything under
  `src/thread_archive` — the 0.0.2 wheel carried ~206 KB of them. They live in
  `host/repair-dumps/` now, `repair_grok_tool_names.py` writes there by default,
  and a new house ratchet (`tests/meta/test_package_tree.py`) fails the suite if
  a dump lands back inside `src/` (and holds `_scripts/` to Python-only).
  Surfaced by a Grok review of the product (thread 3716490).

- **Web viewer: generic 500 body + loopback bind guard (2026-07-14).** The
  cohosted viewer returned `str(exc)` to the client on error (exception text can
  carry paths/SQL) and would bind any `--web-host` silently, though it is
  unauthenticated full read of the archive. Error bodies are now a fixed
  `{"error": "internal error"}` with the detail logged server-side, and
  `serve_in_thread` refuses a non-loopback host unless
  `THREAD_ARCHIVE_WEB_NONLOCAL=1` makes the exposure deliberate. Same review.

- **`DEFAULT_HOME` resolves at call time (2026-07-14).** It was frozen at import, so it
  answered with whatever `$HOME` said then — `_config.default_home()` now.

- **`$HOME` itself is now redirected, not just `THREAD_ARCHIVE_HOME` (2026-07-14).**
  The conftest pinned the archive's own home env var but left `$HOME` alone, so
  anything resolving `Path.home()` by another route (`DEFAULT_HOME`, the launchd paths,
  `~/.thread/logs`) still pointed at the real machine. It is redirected at conftest
  **import** now — before the modules that freeze those paths into constants are
  imported, which a fixture would be far too late to do. A third house ratchet,
  `tests/meta/test_isolation.py` (lockstepped with the others), enforces it.

- **A meta section, and the declared dependency surface is now enforced
  (2026-07-14).** New `tests/meta/` carries the two house ratchets.
  `test_dependency_tiers.py` holds the import law: the stdlib and the product's
  own package are free, a distribution in `project.dependencies` is free, an
  optional extra or a sibling thread product is importable **fail-soft only**
  (under a `try` handling `ImportError`, degrading when absent — dependency
  tier 3), and anything else is not importable at all. A package that is merely
  present in the venv because some other dependency dragged it in is a
  transitive, not a dependency. `test_no_global_patch.py` freezes
  string-target `patch("a.b.c")` at a baseline that only shrinks — the leaky
  form that replaces a symbol process-wide and survives no refactor.
  This surfaced a real gap: `_retrieval/embed.py` and `_retrieval/rerank.py`
  import `torch` directly (to pick the device and the fp16 dtype) but nothing
  declared it — it was arriving as a transitive of sentence-transformers. It is
  now declared in the `embeddings` extra, where it belongs. The single-path
  import ratchet moved into `tests/meta/` alongside the house pair.

- **The viewer's tests fail on an unmocked request, and hold a coverage floor
  (2026-07-14).** The frontend suite faked network with
  `vi.stubGlobal('fetch', …)`: one global stub answered *every* URL the component
  asked for, so nothing could tell a request the test meant to make from a stray
  one, and a component that started fetching something new kept its tests green.
  Network is now faked at the MSW seam (`src/test/mswServer.ts`, helpers in
  `src/test/msw.ts`) with `onUnhandledRequest: 'error'`, matching lab/web. That
  guard alone isn't enough here — the viewer's components catch their own fetch
  errors and render an error state (`Sidebar` swallows outright), so MSW
  rejecting an unmatched request can still leave a test passing; a
  `request:unhandled` recorder in the setup file reds the test in `afterEach`,
  which is the case that was passing silently. Coverage rides the CI row:
  `frontend-test` passes `--coverage`, gating the lines floor in
  `frontend/vite.config.ts` (75, under the measured 75.58% — a ratchet, not a
  target; `Sidebar`, `StatusBar`, `Landing` and `App` are the untested surface
  it's waiting on). A plain local `vitest run` stays fast and ungated.

- **Search is ~5× faster; same results (2026-07-13).** Stage-profiling the
  production pipeline over the eval's own queries showed the cost was never
  the models: the lexical arm was p50 3.0s / p90 11.7s per query while the
  whole vector arm ran ~160ms. Two causes, both in `fts.py`. (1) Every MATCH
  pass sorted with `ORDER BY bm25(event_search)` — an expression sort, so
  SQLite built a temp B-tree and evaluated the SELECT list (snippet()
  re-tokenizes the doc) for *every* matching row; a broad OR-fallback query
  matches 200k–600k docs of the ~1M-doc index, costing 2.3s p50 / 6.7s p90
  per query. `ORDER BY rank` is the same bm25 ordering but engages FTS5's
  internal rank-sort, streaming the LIMIT rows out without the external sort:
  385ms p50 with snippet, provably identical result sets (verified
  set-for-set over the eval queries). (2) The code/pipe-OR shapes'
  substring-LIKE pass is a 2–3s full-table scan that ran even when the
  phrase-MATCH pass had already filled the 200-candidate pool; it now runs
  only on pool shortfall — still always for the rare identifiers it exists
  to catch. The cross-encoder stage (`rerank.py`) loads fp16 on
  accelerators and predicts in batches of 8 (one big batch pads every pair
  to the longest doc): ~4.5s → ~1.6s per reranked query at scores whose
  ordering is bit-identical to fp32 (zero pairwise flips over real pools).
  Measured end to end on the gate protocol: fused sans rerank p50 1313ms →
  591ms (floors hold; MRR 0.515 / recall@10 0.710 on the current sample);
  full pipeline with rerank 9.2s → 2965ms p50 at flat quality (MRR 0.463 /
  recall@10 0.740). Agents feel this directly — `thread_search` reranked
  conceptual queries at ~9s before. Pinned by test: the MATCH plan shape
  (no external sort) and the LIKE shortfall gate, both in test_search.py.

- **Codex turns are attributed to the model that served them (2026-07-13).**
  Every archived Codex turn read `model: "codex"` — a placeholder, not a model.
  The importer resolved the model from `session_meta.model`, and Codex (>= 0.144,
  at least) no longer puts one there: it names the serving model per *turn*, in
  `turn_context.model`, plus a `thread_settings_applied` line on a mid-session
  switch. Those lines were preserved verbatim as content blocks (Archivist, not
  Filter — the data was never lost), but nothing lifted them into the event
  payload, so the one field that answers "which model wrote this?" was a constant
  across the whole provider. Found via a thread where Codex was asked its own
  context limit, couldn't answer from its rollout, web-searched, and guessed wrong
  (claimed GPT-5-Codex/400k; it was `gpt-5.6-sol` at a 258,400-token window). The
  model is now tracked as the line stream is walked — seeded, for an incremental
  resume, from the last model named behind the watermark — and a turn whose
  `task_started` precedes its own `turn_context` is corrected in flight rather
  than left on the stale value.

- **…and the 931 turns already archived under the placeholder are repaired
  (2026-07-14).** `_scripts/backfill_codex_model.py` re-attributes them in place
  across all 36 codex threads: 472 `gpt-5.6-sol`, 459 `gpt-5.5`, none left
  unresolved. Threads carrying preserved `turn_context` blocks answer for
  themselves; threads imported before that preservation existed (which dropped the
  lines entirely) are answered from the on-disk rollout, whose model changes are
  matched to turns *by the clock* — an old import drew its turn boundaries around
  a different set of lines, so its message ids need not be the ones a replay
  computes, but both records share a timeline. Where both sources can answer they
  agreed on every turn; a disagreement is skipped, never tie-broken. Because
  `model` is a dedup-content key, each patched event's `dedup_key` is re-hashed
  onto its new payload — the identity a fresh import now computes, so re-importing
  one of these sessions dedups against the repaired rows instead of doubling the
  thread (pinned by test). Store and truth are rewritten together under the
  reindex lock, the truth line-by-line with every other line passed through
  verbatim; `verify` is clean (3,590,488 events, zero drift) and no row's payload
  fails to re-hash to its key. Row-level undo record committed beside the script.

- **Retrieval quality is CI-gated (2026-07-13).** Search-quality drift was the
  one regression class nothing watched: verify proves no byte is lost, but a
  ranking change that quietly craters recall passed every gate (the
  live-capture FTS gap sat through every green nightly), and the eval harness
  ran only when someone remembered. A `retrieval-gate` ci.toml row now runs
  `scripts/retrieval_eval.py --auto-titles 100 --rerank off` against the live
  archive on every sweep (read-only, ~2.5 min) and fails on floor breach —
  MRR ≥ 0.40, recall@10 ≥ 0.62, a ratchet calibrated under measured (0.464 /
  0.710). The harness grew the floor args (`--min-mrr`, `--min-recall10`,
  `--min-recall20`; breach → exit 1) and a `--lexical-only` mode measuring
  the extra-less core install. Found while wiring it: `embed._encode` never
  consulted `is_available()`, so the module's documented degrade contract —
  availability off means the vector arm sits out — didn't hold at the one
  seam that matters in a populated home, query embedding via
  `vectors.search` (the model-free test suite never noticed because an empty
  vector store short-circuits earlier). The check now lives in `_encode`
  itself, pinned by test; measured full-pipeline reference numbers from the
  same calibration: fused + rerank MRR 0.421 / recall@10 0.705 at 9.2s p50
  per query, fused sans rerank 0.464 / 0.710 at ~1.4s p50.

- **The NAS nightly's failures were macOS TCC, not smbfs — and stage errors
  now say so (2026-07-13).** Every post-07-12 nightly against the FrezFamily
  NAS failed backup + restore-drill with EPERM on ordinary operations
  (scandir of `.generations/`, reading the mirror's `kg_events.jsonl`,
  creating dot-tmp files) that all succeed from an interactive shell.
  Reproduced under a launchd-submitted job: macOS TCC blanket-denies
  network-volume access to background job contexts that lack their own grant
  — the terminal app's grant covers interactive shells, which is exactly why
  the 07-12 session's hand-run drill passed while every 04:00 run failed.
  Fixing it needs a one-time System Settings grant (Privacy & Security →
  Files & Folders → Network Volumes) for the job's interpreter; what the code
  can do is stop the misdiagnosis: nightly stage errors are formatted through
  `_stage_error`, which recognizes the EPERM-on-darwin shape (errno 1, vs a
  real permission denial's EACCES) and appends the TCC diagnosis and fix to
  the error that rides health.json and the failure notification.

- **Writers ride out maintenance transactions instead of erroring
  (2026-07-13).** The watcher's `database is locked` poll errors (44 in the
  log, in bursts on 4 days) all coincide with in-place maintenance writes —
  an FTS rebuild over the full corpus holds SQLite's write lock for minutes,
  and the store's 5s `busy_timeout` was sized for ordinary commits.
  `busy_timeout` is now 60s: under WAL only writers ever wait, so the read
  path is untouched, and a blocked ingest converges instead of failing into
  health and retrying a poll later. The failure was never lossy — events and
  watermark commit as one transaction, truth is written ahead, dedup collapses
  the retry — but that self-heal contract was design-implied and untested;
  `test_locked_out_commit_self_heals_on_next_poll` now pins it (a commit
  killed by SQLITE_BUSY costs exactly one poll of latency, index converges to
  exactly-once, verify green).

- **The sanctioned off switch can't double as a silent capture hole
  (2026-07-13).** Coverage's report-only `disabled` block now stats each
  disabled source's store (items + latest activity) alongside its import
  history: a deliberate opt-out's store staying active is normal, but a
  source disabled by accident — a config bug, a wizard regression — had no
  surface where its unarchived activity showed. Still never red (intent
  isn't machine-decidable); the hardening is visibility plus write-path
  pins: skipping the wizard's import step wholesale writes zero opt-outs
  (only an explicit per-source "no" may write `enabled: false`), and the
  full watcher set is injectable (`all_watchers`) so stub-driven coverage
  tests never discover the real machine's stores.

- **The parser island's failure behavior is pinned (2026-07-13).** The
  provider goldens lock each importer's output for well-formed input;
  `tests/test_parser_robustness.py` now locks what happens to damaged input,
  across all five line-stream providers (claude-code, cloth, codex, grok,
  antigravity): a truncated or junk line never aborts the intact lines
  around it, garbage never fabricates events, parse drops are counted where
  the pipeline accounts for them, unknown line types don't abort (and on
  claude-code are preserved as events), and a damaged-then-repaired file
  re-imports to convergence through the content-hash cursor rewind — no
  duplicates, nothing pinned past the repair. All 27 cases passed on
  arrival; the suite exists so a parser refactor can't quietly regress the
  properties that happened to be true.

- **Live-capture assistant text is searchable and readable (2026-07-13).** 402
  threads (cloth, loom, needle, officiant, librarian, …) stored assistant text
  only as `text_delta`/`thinking_delta` token events plus the assembled
  `api_request_completed.content_blocks` summary — none of which the FTS
  extractor indexed or the readers rendered: `thread_search` couldn't surface a
  word those agents said, `mode='chat'` showed empty `[ASSISTANT]` headers, and
  `mode='full'` rendered per-token lines. One rule now serves both surfaces: an
  api_call's content comes from its `text_complete`/`thinking_complete` twins
  when they exist, else from the summary — matched by api_call_id *and* by text,
  because a doubly captured session (file import + live stream) carries the
  twins under different api_call_ids than the stream's summaries. Reader side:
  `read._absorb_stream_deltas` synthesizes the twins (or stitches orphan deltas
  from a stream killed mid-call); index side: a twin gate in `fts.index_events`
  / `rebuild_fts` plus a rebuild-only sweep for arc-less calls (a stream killed
  before its summary arrived indexes its stitched text at the block's last
  delta event — the reader's anchor for the same text), with deep verify's
  coverage check taught the same rule. Backfill was an in-place `rebuild_fts` —
  no truth change, no reindex; 401 of the 402 threads now carry assistant text
  in FTS (the 402nd has only whitespace deltas, nothing to index). Also
  classified the formerly-`unknown` machinery types (`hook_context`,
  `tool_execution_started`, `archived_duplicate` → skipped; `model_change` →
  one legible line).

- **Writers reconnect to a swapped index.db on ingest-lock acquire
  (2026-07-13).** `reindex` publishes by renaming a fresh build over
  `index.db`; the swap check lived only in `open_archive`, which a one-shot
  writer runs *before* blocking on `shared_ingest_lock()` — and the blocking
  wait is precisely the window in which the swap happens, so an import that
  started during a reindex reported success while committing into the orphaned
  pre-swap inode (durable in truth, absent from the live index; reproduced
  end-to-end). The identity check (`_store.reconnect_if_swapped`) now runs on
  every ingest-lock acquire — both variants — where the shared hold guarantees
  it stays valid; `open_archive` keeps a copy for read-path convergence, the
  daemon's hand-rolled reconnect flag is gone (it also missed any reindex that
  fit inside one poll sleep), `checkpoint()` is self-locking (closing the
  nightly backup's unlocked call), and `_knowledge`'s `_locked_write` lost its
  caller-session lock bypass (nested shared flocks coexist, so there is nothing
  the bypass was needed for).

- **One session-id resolver (2026-07-13).** The MCP reader resolved session
  uuids via `Thread.source_id` only; the web viewer's `resolve_archive_link`
  via `ImportState` only — and neither table is a superset (export importers
  never write ImportState; a claude-code compaction continuation's uuid exists
  only there, and that continuation uuid is exactly what an agent inside the
  session holds), so 98 live session ids resolved in the viewer but returned
  "not found" from `thread_read`. Both now resolve through
  `_store.resolve.resolve_session_source_id` — Thread first, watermarks second,
  one separator-suffix matcher — with a cross-surface contract test. The web
  path deliberately keeps no integer-PK branch (its callers spray candidate ids
  that must never land on an unrelated PK).

- **Capture blind-spot detection (2026-07-12).** The loud capture failures
  (exceptions) already reached `watch_errors_last`; the silent class — content
  consumed without a trace, sources gone dark without an error, a wedged
  ingest loop indistinguishable from a quiet day — had no detector. Four
  mechanisms, all reconciling the two ledgers that already existed (what the
  store says happened vs what the archive ingested): (1) parse-dropped line
  counts now ride import results into the watch loop's accounting instead of
  dying in the log; (2) a capture-skip ledger (`<home>/capture-skips.jsonl`)
  records every watermark advance past never-imported lines, making zero-yield
  consumption auditable and reversible while the source file survives; (3) a
  watch-pass heartbeat (`watch_pass_last` in health.json, throttled, with
  per-source cumulative yield counters) disambiguates a healthy quiet loop
  from a dead one; (4) `archive coverage` — a new nightly stage after the
  drill — reconciles each enabled source's store against the archive:
  went-dark (store missing with real imported history) and stale-ingest
  (store activity newer than the newest archived *event*, deliberately not
  the watermark, so watermark-advancing zero-yield drift still trips it) fail
  the night; never-ingested warns; disabled/unwatched sources report ages
  only. Live SQLite stores are exempt from staleness (mtime churn without
  content — `store_mtime_tracks_content`). Found along the way: the CC parser
  already preserves unknown line types verbatim, so type-drift cannot consume
  content on the claude-code path; the consuming paths are the line-stream
  providers' importable-content gates, which the ledger now covers.

- **Backup kit works on SMB destinations (2026-07-12).** Three smbfs
  incompatibilities found bringing up the FrezFamily NAS dest, each failing a
  nightly stage: (1) `mkdir(exist_ok=True)` of an existing `.generations/`
  returns EPERM (not EEXIST) on smbfs, failing the backup stage every run
  after the first — replaced with an `is_dir()` guard, and the whole dest-side
  snapshot setup moved inside the degrade path so an unusable gens dir reports
  `generation_error` instead of failing the stage (its own documented
  contract); (2) the restore drill's `copytree` (and the generation snapshot's
  no-hardlink fallback) used `copy2`, whose metadata replication EPERMs
  reading system xattrs (`com.apple.provenance`) off smbfs — both now copy
  content only (`copyfile`; generations and drills consume JSONL content,
  and generation retention is keyed by directory name, not file mtime);
  (3) hardlink-less shares were already handled by the copy fallback.

- **Store-contamination incident (2026-07-11) cleaned up; reconciliation drops
  unanchored citations (2026-07-12).** A test-suite run on 2026-07-11 ~23:21Z,
  before the `_isolate_archive` autouse sandbox landed in `tests/conftest.py`
  that evening, leaked into the live store in two directions: 3,220 truth
  lines (9 real Claude Code sessions re-imported under fixture-allocated
  thread ids 2/3/5/6, all duplicating conversations already archived under
  their real ids — including 951 event-id collisions with the ancient threads
  those files belonged to) and one synthetic `grok:conv-ok` fixture thread
  written into the live index (id 3716440 + 5 events; the watcher's next
  checkpoint then snapshotted its thread record into truth). The nightly
  caught it: shallow verify flagged the drift and the restore drill's
  relational gate refused the rebuild. Cleanup: rogue truth lines quarantined
  to `truth/quarantine/rogue-import-20260711/` under the truth-write lock, a
  salvage reindex dropped the fixture events, and a librarian pass's two
  citations of the fixture event (added while the damage was live) exposed a
  reconciliation gap — a citation whose event *and* thread are both gone from
  a rebuild kept a dangling declared `thread_id` FK that would fail every
  future rebuild's relational gate. `_reconcile_collapsed_citations` now
  drops such unanchored citations (archived or not), reported as
  `citations_dropped_unanchored`; event-only dangles stay tolerated for
  `verify --deep` to report, as designed.

- **More dead vendored code removed (2026-07-11).** Same sweep as the
  exporters deletion: `_thread_import/schemas/` (JSON-schema validation, zero
  consumers), `_thread_import/api.py` (`ConversationMeta`/`ImportSource`, zero
  consumers), and `parsers/pipeline/` + `parsers/transformers/` (the
  `ParserPipeline` architecture was never instantiated by any live parser;
  `ToolCoalescingTransformer`'s tests went with it). The wheel no longer ships
  the schema JSON data files. Also fixed a stale docstring pointer in
  `jsonl_log.reindex` to the deleted reindex-atomic-swap plan, and two
  parent-project references (`apps.chat_import`, `backend/event_log.py`) in
  the vendored island's docstrings.

- **Web viewer caches hashed assets (2026-07-11).** `assets/*` (Vite
  content-hashed filenames) now serve with `Cache-Control: public,
  max-age=31536000, immutable`, so reloads stop re-fetching the bundle.

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
