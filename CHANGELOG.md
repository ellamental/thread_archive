# Changelog

## Unreleased

- The retrieval surface is ratcheted across every door it has. `thread_search`'s parameter list was written out in
  six places by hand, and only one leg of that — the wire descriptions — was checked; the others could drift in
  silence, and had. `tests/meta/test_retrieval_surface.py` now derives all of it from the tool's own signature:
  every parameter has a CLI flag, every flag reaches a parameter, and every scope lands in the usage row. The
  exemptions are declared with their reasons and asserted in both directions, so one that stops being true reds
  rather than going stale.

- `repo=` was missing from the search usage record, so a `commit=` scope resolved against an explicit repository
  wrote a row no eval could replay. It is recorded now, and the ratchet above is what keeps the next one from
  happening quietly.

- The CLI's delegated search builds its arguments from `thread_search`'s signature instead of a second list of
  parameter names. A filter that list forgot would have been dropped on the way to the warm server and come back as
  a *wider* search, ranked and rendered — the failure shape delegation has that nothing else does.
  `test_cli_delegate.py` drives every flag at once against a live server and asserts the wire.

- The provider plugin API ships a conformance kit, not just a golden harness. `assert_provider_contract` holds a
  descriptor and its watcher to what archive reads them under — a stable name, resolving `follows` / `parser_id`,
  something that can actually feed the provider, an `export` spec whose `detect` declines rather than raises, and a
  watcher that constructs, agrees with its own `discover()`, and stays quiet with its store absent (the normal case
  on almost every machine). `assert_reimport_adds_nothing` holds an importer to the invariant the whole ingest loop
  rests on. Both are dogfooded: `tests/test_provider_contract.py` sweeps every built-in, so a provider added
  tomorrow is covered the moment it is registered, and the per-provider idempotence tests now run the shipped
  helper over their own fixtures instead of counting rows by hand — which added the watermark check they never had.

- The viewer dispatches through a route table (`_web.server.ROUTES`) instead of a 250-line chain of path
  comparisons. A chain can only be executed; the surface has readers that need to *enumerate* it — the public-URL
  pin, which was keeping a hand-written list beside it, and the rules every endpoint is now held to at once: each
  route resolves to itself, none shadows another, a write is reachable only by POST and a read refuses one, and no
  route escapes `/api/`. Those hold for endpoints nobody has written yet, which is what a test per endpoint cannot
  do. Behaviour is unchanged — every handler kept its logic and its reasons; only dispatch moved.

- The retrieval pipeline's stage contract is asserted. Every hit annotation is read with a zero default
  (`result.get("_rrf", 0.0)` and its neighbours in `rank`) and every display field with an `or` fallback, so an arm
  that stops annotating does not raise or log — it flattens one term of the score for every hit and the search goes
  on answering, worse. `tests/test_hit_stage_contract.py` drives the real pipeline and pins what each stage leaves
  behind: the lexical arm's two scores and their normalization, that no hit claims a score no arm computed, that
  fusion annotates everything it returns, that enrichment reaches every hit, and the browse and code-axis column
  sets the renderer branches on. A type per stage would not have caught any of it: the stages mutate one dict in
  place, so each seam would need a `cast`.

- Opening an archive no longer writes to the process environment. Which home this process is working in is the
  archive layer's own state (`_config.pin_home` / `pinned_home`), consulted ahead of `$THREAD_ARCHIVE_HOME` and
  released when the archive closes. `os.environ` is shared with every other library in the process and inherited by
  every child it spawns, so selecting a home there made *reading* an archive a side effect that outlived the read —
  and, worse, made the operator's setting unrecoverable afterwards. Two call sites were already defending against
  it by hand: the v2 truth migration saved and restored the variable around itself, and the setup machine probe had
  to refuse env-mediated resolution when asking whether an installed agent covers a home. Both now say what they
  mean. Resolution order is unchanged in every other respect — explicit argument, then the open archive, then env,
  then the default.

- The truth↔index invariant is a property, not a set of examples. `tests/test_truth_index_invariant.py` drives a
  real archive through generated sequences of imports, re-imports past a watermark, amendments, checkpoints and
  rebuilds, and asserts after every step that `verify(deep=True)` is green, that a rebuild reproduces the index
  exactly, and that no event the archive holds becomes unreachable. The oracle is the product's own deep verify;
  what the file adds is the orders nobody wrote down — and a shrunk counterexample when one of them breaks.

- Opening and closing the archive moved to `_lifecycle`, the layer their dependencies are already on. `open_archive`
  reaches exactly three things — resolved paths, the SQLite store, the truth log's append handles — so the ops kit,
  the watcher and the composition layer were all reaching *up* into `_api` to call it, seven sites of the nine that
  made `_ops -> _api` the largest tier violation. `_api` re-exports both, so every caller that opens an archive
  before composing an operation is unchanged. What is left of `_ops -> _api` is the restore drill reading and
  searching the home it just restored, which is a deliberate end-to-end round trip rather than a layering slip.

- The CLI's duration formatter moved to `_fmt`, a leaf. The setup wizard was importing a front door to borrow it —
  the whole of the `_setup`/`cli` cycle, now gone. `_ops.notices` keeps its own age formatter: it renders at the
  resolution an operator alert is acted on, and collapsing the two would change what one of the surfaces prints.

- Internal layering: 9 upward edges over 20 sites down to 7 over 13, and two mutually-importing clusters down to
  one. `_lifecycle` sits inside the remaining cluster only by way of `_truth -> _ops -> _api` and leaves when that
  edge does. The ratchet records the largest remaining lever, which is not in its baseline: `_ops.ledger`,
  `_ops.telemetry` and `_ops.load_runs` are instrumentation primitives depending on nothing above `_config`, filed
  under a tier-3 package that tier 2 then reaches up for.

- The open archive is an object. `_store._instance.Archive` holds the engine together with the per-archive scratch
  every layer above caches in (`Archive.cache(name)`), and `close()` drops the whole set in one step — so
  `close_engine()` is the entire teardown and a cache added above the store needs no new line anywhere. The vector
  matrix, the corpus graph and the exact-set memo moved into it; the truth-log append handles and the ingest-error
  tally stayed process-global, the first because it is path-keyed with real close semantics reached from rollback
  paths under the truth-write mutex, the second because counting *this process's* sightings is what it is for.
  `use_engine` now opens a transient archive around the caller's engine rather than swapping a bare engine, so a
  block running against another index file caches into its own slots and drops them at exit.

- The identity those caches are scoped by is no longer an address. They keyed on `id(get_engine())`, and CPython
  reuses the id of a disposed engine — so a fresh archive could match a closed one's key and be served its matrix,
  graph, or set memo, on a collision nobody controls. `Archive.token` comes from a counter that never repeats, and
  because a cache now lives *inside* the archive there is no surviving dict for a dead one's entries to sit in
  either. Two archives can be open at once and keep separate everything, which the module globals made impossible;
  `tests/test_archive_instance.py` pins that, the non-recycling tokens, and the teardown.

- `current_archive_or_none()` — a peek that never opens. Opening resolves the home from the environment and pins it
  for the process, so a function that opens as a side effect of doing nothing *chooses a home*, and a later
  deliberate choice then silently has no effect. The cache resets hit exactly that: routed through
  `current_archive()`, they turned the test suite's own isolation fixture into a home-pinning call and pointed
  tests at the default home instead of the one they had asked for. The resets are no-ops when nothing is open, and
  the regression is pinned per reset function.

- The suite's isolation fixture lost its per-global reset ritual — closing the archive covers the matrix, the graph
  and the memo — and resets the retrieval surface through `set_default_surface` rather than by writing the module
  global.

- The internal package graph is ratcheted, the way the external dependency surface already was. Every package under
  `src/thread_archive` now sits in a declared tier (`tests/meta/test_internal_tiers.py`) and may import only its own
  tier or a lower one, with the graph as a whole required to stay acyclic; two shrink-only baselines freeze what
  exists today — nine upward edges over twenty sites, and two mutually-importing clusters. The scan reads every
  import wherever it sits, function-local ones included, which is the point: a deferred import hides a cycle from the
  module loader without removing it, and the package had 317 of them against 159 module-level. `if TYPE_CHECKING:`
  imports are exempt. A planted `_store → _mcp` import fails both assertions, so the ratchet demonstrably bites.

- Two layering violations paid down, which is what took the largest cluster from thirteen packages to ten. The store
  no longer reads the provider registry to resolve a session id: `resolve_session_source_id` takes the `source_id`
  separators as a required argument and `_providers.resolve_session_ref` is the paired entry point that supplies
  them, so `_store` now depends on nothing but `_config` and the union that keeps the MCP reader and the viewer
  answering alike stays in one function. The front-door label moved down to the ledger that carries the field:
  `_DEFAULT_SURFACE`, `serving`, `current_surface` and `served_by` live in `_retrieval.usage` beside `UNATTRIBUTED`
  (whose docstring already argued a reader should be able to learn the vocabulary without importing the tool
  surface), and `_tools` re-exports them — so the background warm pass names its own door without retrieval
  reaching up into `_tools`. The test suite's own reset now goes through `set_default_surface` rather than poking the
  global.

- `py.typed` ships. The package was fully annotated at 87% and a type checker was ignoring all of it (PEP 561), which
  landed hardest on the one public Python surface: a provider plugin written against `thread_archive.provider` got no
  checking at all. The four unannotated functions in `provider/testing.py` — the fixture, `normalized_truth`,
  `assert_golden`, `write_jsonl` — carry annotations now, and the wheel lane asserts the marker is in the artifact.

- `_api`'s docstring described a chokepoint that does not exist. It claimed to be "the single coordination surface
  every caller goes through" while `cli.py` reaches it at 18 sites and goes past it into the layers at 30. It is
  documented as what it is: a composition layer where a multi-layer operation is assembled once instead of at each
  caller, with other callers expected to reach the layers directly.

- A second pass over retrieval's rationales, against what the code and the ledger actually do. `_tools.thread_search`
  justified its `page` clamp with "the pool is sized from page*limit" — the pool has been `max(limit*5, pool_floor)`,
  deliberately page-independent, since paging became slices of one ordering, and the engine's own comment says so;
  the clamp stays (200 is the deepest page any limit can reach) with the real reason. `_probe.SET_OUTCOMES` sold a
  three-way split as the diagnostic for a slow `set_ms`, but `set_deltas` is `fts.matched_threads`' outcome and the
  served path calls only `count_matches`, which has no delta path — zero rows in the ledger since the counters
  shipped, and structurally never any. `matched_threads` itself still claimed to be "the query that makes a complete
  answer possible" with no call site in the package. `coherence_gamma` called `COHERENCE_GAMMA` "the swept default"
  three lines from the constant that documents it as never resolved by a sweep. `browse_threads` advertised exact
  pagination with "nothing cut, every row reachable" while its own code-axis branch reports `capped` at
  `_CODE_AXIS_CAP`. The default-scope comment claimed the tool-output exclusion "measured better rather than merely
  cheaper", a measurement nothing on this box records; `_extract`'s structural argument (density is IDF-blind, so
  the ranker cannot discount a grep dump itself) is what survives. Stale magnitudes dropped where the corpus has
  moved past them: an "814MB" base rebuild, "400k tool payloads", a "~300 char" mean embedded doc (578 now).

- The model half of the deferral fork is stated rather than inferred. A warming server sits the vector arm out twice
  over — once because the model is not resident, once because the matrix is cold — but only the second had a flag, so
  a search that served lexical-only for want of a model was distinguishable from one that embedded fine only by
  reading an `embed_ms` near zero. It now stamps `embed_deferred` on the search row, set at the load-policy gate in
  `embed._encode`, which is the only place that can tell a deferral from the other ways an embed returns `None`
  (models switched off, a cached load failure). The same window was also being *mis*labelled: `embed_cold` — "this
  query paid the tens-of-seconds load" — was sampled as available-and-not-resident, which under the deferral policy
  is exactly the query that pays nothing, so every fast degraded search was landing in the cold-model band that
  `search_lab/latency_replay.py` reads. The sample now excludes deferring processes.

- `summary` is gone from retrieval as a content type. Nothing writes one — no `event_search` row, no vector, no
  thread-meta doc (the meta sync's unnamed-doc collector swept the last of them) — so its ranking multiplier in
  `rank._CONTENT_TYPE_WEIGHT` and its slot in the corpus graph's centroid pools (`embed_graph._CTS`) were scoring
  and scoping a type that cannot appear. `_CTS` rides the persisted graph's build shape, so the next build rewrites
  the cache once; the partition it produces is identical, since there were never any summary vectors to drop. The
  modeling stays exactly as it was: `Thread.summary` / `Thread.indexed_summary` are stored, verified against truth,
  and readable through `thread_read(summary='short'|'indexed')` — and still deliberately unindexed, which
  `test_summaries_are_never_searchable` holds.

- Retrieval's rationales are audited against what the code and the lab actually do. The coherence signal no longer
  cites a log-mined click protocol, a per-topic gold bench, or a `search_lab/graph_eval.py` — all retired or
  deleted, so nothing on this box could re-derive the numbers they quoted; `COHERENCE_GAMMA` is now labelled
  inherited-and-not-re-derived, the way `params.py` already labels the ranking weights. `_contention` claimed the
  vector matrix is "read whole into memory", which `vectors.py` stopped being true of when the pack became an mmap.
  Three docstrings described paging as resolving its match set through `fts.matched_threads`, which the shipped
  package has no call site for (the search path uses `count_matches`, and `set_deltas` has never once appeared in
  the usage ledger); `rank.py` justified `thread_evidence` by a per-thread grouping stage the pipeline does not
  have. Also dropped: the unused `_PATH_EVENT_TYPES` / `_COMMIT_EVENT_TYPES` / `_PR_EVENT_TYPES` constants in
  `code.py`, whose comment described event types the fold reads while the fold spelled them inline.

- The KNN matrix's cold-cache inline build honors the deferred-construction policy: a warmed server's request
  thread never assembles a pack (it kicks the single-flight background refresh, serves lexical-only, and stamps
  `matrix_deferred` on the search row), which closes the last inline-build hole — measured in the ledger at up to
  24s inside a request racing the warm pass. The warm pass now primes the matrix as its own recorded stage
  (`matrix_ms`) rather than as a side effect buried in the priming search's time. Base-pack builds serialize on a
  machine-wide flock (two rivals measured ~156s each against ~20-40s alone; the loser now usually mmaps the
  winner's published files), and pack assembly decodes all vector blobs in one `frombuffer` pass instead of an
  ndarray per row fed to `vstack`. Pack tmp files are named by pid *and* thread id: the matrix refresher and the
  corpus-graph build share a process, and with pid-only names one thread's `os.replace` could consume the other's
  half-written tmp (observed as a `FileNotFoundError` killing a graph refresh; the build flock also serializes them).

- `thread-archive search` delegates to the shared HTTP MCP server when one is alive and serving the same (default)
  archive home (`_delegate.py`): one stateless JSON-RPC `tools/call` POST to :8788, so a terminal search rides the
  warm process (~0.5s measured) instead of paying the in-process model load (~6s same-minute). Fallback is the
  contract — nothing listening, a timeout, malformed answers, and tool-level errors all re-run in-process, keeping
  the CLI's own error rendering and exit codes; `--local` forces it, `THREAD_ARCHIVE_NO_DELEGATE=1` disables it, and
  `THREAD_ARCHIVE_MCP_URL` points at a nonstandard port. A delegated call's serve row carries `delegated: true` (the
  engine's search row is the server's, unattributed as ever). `read` stays in-process on purpose: it loads no model,
  and its exit code resolves the ref locally.

- The docs tree is inverted: the shipped manual is `docs/public/`, and `docs/*.md` is the maintainer's half
  (releasing, benchmarks, the dev panels). The wheel names `docs/public` in its include rather than excluding an
  internal directory, so the artifact is default-closed — a new page ships only if it was deliberately written under
  `public/`, where before a maintainer's page shipped unless someone remembered to exclude it. Both readers
  (`thread-archive docs`, the viewer's `/docs`) and the viewer's link rewriter follow the move; the rewriter now
  resolves a page-relative path against the manual's location rather than assuming one level below the repo root.

- The claude-science incremental slice counts raw store rows, matching the watermark: it sliced the *filtered* line
  list with the raw-row offset, so any row the importer skips (a non-user/assistant role, undecodable JSON) shifted
  every later pass by one and silently dropped that many of its newest messages — permanently, since the watermark
  still advanced to the raw total. The live store had no filtered rows yet; the loss was latent.
- The cursor/opencode "unchanged" gates re-scan anything written within ten minutes of the import stamp
  (`store_write_settled`): the stamp is our wall clock, written after the read, so a write landing between the two
  carried a store timestamp older than the stamp and was skipped on every later poll — for a conversation that ended
  there, its last turn was lost for good. Re-scans inside the slack are no-ops (row cursor + cross-pass dedup).
  Cursor's `state.vscdb-wal` is now watched too, like opencode's and claude-science's, so a write sitting only in the
  WAL moves the fingerprint.
- Search hits end in `· source · date` — the docs said "read the latest date off the matches" and the render carried
  no date — and snippets/context lines are clipped at 400 chars (the web viewer already capped; the agent surface
  returned whole messages when a hit had no newlines). `since`/`until` accept `2h`/`7d`/`2w` and **reject** anything
  else by name: the bound compares lexicographically, so an unparseable value passed through raw was a filter that
  silently matched nothing. The subjects docstring stops advertising `thread_search(topic_id=...)` — the engine scope
  is real, but exposing it as a tool parameter is ratcheted out (`test_public_api.py`), a standing product decision.
- Retrieval docstrings stop describing the removed cross-encoder stage as the architecture.
- The ingest-error tally is reset per test, and the suite runs serially in its own CI row (`pytest-serial`). The tally
  is a process global that only writes a signature's first sighting and then powers of ten, so a count one test left
  behind silenced the next test's identical fault — a real red under `-n 0` (and under `-n auto` on a one-core box,
  which is how the Docker install lane could have found it) that `--dist=loadfile` hid by putting the two files on
  different workers. Serial is the only shape that can see cross-file state leaks at all.
- `_fsync_handle` takes its `fsync` call as an argument, so the durability of a bulk batch is asserted rather than
  assumed: past `MAX_OPEN_HANDLES` the early handles are evicted and must be reopened and synced by fd, and a body
  that skipped the syscall entirely was indistinguishable from the real one to a test that only checked it returned.
- Pinned the boundaries a mutation probe walked through untouched: the read chunker admits a turn that exactly fills
  the char budget, the summary view calls `offset == total` past-the-end, and RRF's fused scores are the documented
  `Σ 1/(k + rank)` on 1-based ranks, peak-normalized, with ties broken on `event_id` so arm order cannot reshuffle a
  page. The lexical quality floors move to MRR 0.95 / recall@5 0.95 — one case of headroom over a corpus the stack
  solves perfectly, where 0.85/0.90 absorbed six.
- Coverage floors ratchet behavioral coverage, not presentation: `cli` 98→88, `_docs`/`_viewer` 100→95, TOTAL 94→93,
  and `test_cov_cli.py` keeps one representative failure shape per operator report (24 branch-enumeration tests
  removed) — the exit-code contracts and everything driving real machinery stay. Every floor — Python packages and
  the frontend's four dimensions — then drops a further five points: slack for ordinary edits, so only a real
  regression reds a row (`cli` lands at 83, TOTAL at 88, frontend at 85/74/86/89).
- The CI latency gate (`latency-gate`, `search_lab/latency_smoke.py`) is removed, with `speed.smoke_set`/`ceiling_ms`
  and its `smoke` baseline: it measured the live, growing archive, so a red could not distinguish growth from a code
  slowdown and its answer was to re-seed itself. Latency is watched through the retrieval usage telemetry and measured
  deliberately with `latency_replay.py`.

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
