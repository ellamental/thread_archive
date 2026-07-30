# Changelog

## Unreleased

- **A repaired drift stops asking to be repaired — `thread-archive source recheck`.**
  Degradation verdicts were derived from a 7-day rolling window over an append-only
  ledger, and nothing in the system could ever say a record had been *dealt with*.
  So a successful repair changed nothing the operator could see: every search kept
  prepending `note: <source> import is degraded`, naming a remedy that had already
  run, for the rest of the window. The ledgers now take a third record kind — a
  **resolution** — and closed observations drop out of the counts coverage degrades
  on while staying in the file, in `total`, and in `recent` (a repair must not be
  able to erase the drift it repaired; that trail is what a later regression is read
  against). Closed by evidence and not by assertion: the only writer is the
  ledger-driven re-import, the only thing it closes is a file it actually re-read
  through the current parser, and its stamp is taken *before* the re-read — so
  findings the re-parse itself records land after it, stay open, and keep the source
  degraded. A repair that didn't work closes nothing. `source recheck <provider>`
  runs that re-read on its own, which the recovery path previously had no way to do
  without a patch — a parse fix arrives by core release at least as often as by
  local patch, and the operator who upgraded into one had no way to retire a verdict
  their upgrade had already fixed. It reports what it could not reach (a file the
  provider pruned with no quarantine copy is unfalsifiable, and saying so beats
  silence), refreshes the cached verdict every surface reads, and exits non-zero
  naming `source fix` when the drift is live. The verdict's remedy now comes from
  the verdict: three of the four reasons point at `recheck`, and `went_dark` — a
  store that is simply gone, with nothing to re-read — no longer advises a patch
  scaffold that cannot help it.

- **The web viewer is dev-only now — it runs from a clone and ships in no wheel.**
  `_web` (the stdlib server plus the committed React bundle) was 23% of the
  download and 28% of the unpacked install, a browser UI every `pip install` paid
  for whether or not it ever opened one. What an install is for is preservation,
  retrieval, and the MCP server; reading through a browser is a thing a checkout
  does. `_web` now sits in the wheel `exclude` beside `_dev`, and the
  `artifacts` key that force-included `_web/static/` is gone — it re-includes
  what `exclude` drops, so leaving it would have silently undone this.
  Same-shaped as `_dev` and for the same reason, one tier up: the surface is
  absent from an install rather than present and broken.

  The part that is not just packaging is that nothing offers what it cannot
  serve. `thread_archive._viewer.viewer_available()` is the single probe, and
  every offer of the viewer goes through it: `build_parser` registers the `web`
  verb, `watch --web`, and `service install --no-web` only where the viewer
  exists (so an install's `--help` lists neither the verb nor the flags, and the
  epilog loses the line pointing at them), `setup` offers a browser and a
  `/upload` URL only there, and `watcher_spec` writes no unit carrying `--web`
  without one. That last is the sharp edge: a unit with a flag its own argparse
  rejects is exit 2 on every start, which launchd and systemd read as a service
  to restart forever. `tests/test_viewer_probe.py` drives both answers, and the
  package lane asserts the wheel carries no `_web`, that importing it fails, and
  that `web` is absent from an installed `--help` — the same rule the measurement
  verbs already answer to: an install advertises no verb it cannot run.

  The viewer stays in the sdist: it ships `tests/`, and `test_web.py` imports
  `_web`, so dropping it there would ship a red suite — the invariant
  `test_sdist_keeps_the_search_lab` already holds for `search_lab/`. Nothing
  changes for this machine, whose watcher runs from an editable checkout and
  cohosts the viewer exactly as before; the viewer's URLs stay steady for the
  editor buttons and sibling navbars that link them, now as a promise to the
  family rather than a public interface — `docs/stability.md`'s public API is
  four things.

- **The retrieval tools cost ~3,150 tokens of every agent's context before anyone
  searched anything; now they cost ~930.** An MCP tool description is charged to
  every session that lists the tools, called or not, and `thread_search`'s was 7.6
  KB of good pedagogy — the code axis and commit blame in full, enumeration
  semantics, browse recipes — for a corpus of features the usage ledger shows in
  ~2.5% of searches. The wire description is now each tool's compact contract
  (`SEARCH_DESCRIPTION` / `READ_DESCRIPTION`), passed explicitly to FastMCP rather
  than lifted off the docstring; the long form stays where it was, beside the code
  that answers it, and a third tool `thread_help('search'|'read')` serves it on
  demand to the caller that wants it. What the compact form gives up is grammar,
  never a name: every parameter is still named where an agent can see it, and
  `tests/test_mcp.py` reds if one isn't. The advice that used to be preamble now
  arrives as output — a search that finds nothing, or only nearest-neighbour
  guesses, offers the retries that would plausibly change *that* result
  (`match='substring'` for a bare token, `path=` for a filename-shaped query). Also
  removed: `thread_read`'s `Args:` block, ~390 tokens restating prose from the same
  docstring, which this MCP SDK never turned into schema descriptions anyway.

- **The code axis has a third strand: `thread_search(pr=…)` finds the sessions that
  worked on a pull request.** Claude Code emits a `pr-link` line naming the PR a
  session is on; the parser had never modeled it, so it went down the verbatim
  preservation path and — one marker per turn, 22 of them in one session — drove
  claude-code to a `validation_drift` degradation verdict off a single afternoon.
  It is now a modeled `pr_link` event folded into an `event_prs` projection beside
  `event_paths` and `event_commits`. Not a commit lookup by another name: a PR is a
  unit of *intent* where a commit is a unit of *change*, so the scopes disagree on
  purpose. A commit's contributors are inferred from file overlap inside its
  authorship window; a PR's are stated by the harness — no window, no corroboration,
  no reachable repository, and it reaches the sessions that left no commit in the
  branch at all. Takes a bare number, `owner/name#4`, or the URL off the address
  bar; a bare number matching several repositories returns all of them and says so
  rather than silently picking one. `CodeCursor.projection_version` is bumped, so
  existing archives refold all three strands on the next pass.
- **The ledger-driven re-import did nothing, and said it worked.** `source fix
  --activate`'s recovery half is what makes "however late the fix, nothing that
  reached a ledger is lost" true, and two separate mechanisms were each enough to
  neutralize it. It deleted the import watermark to force a re-read, but an absent
  watermark on a thread that already has events is precisely the signal for the
  importer's adoption guard, which re-stamps it at EOF and imports nothing — so the
  stronger-looking move did strictly less than nothing. And it recovered by polling
  the source, while a watcher skips an unchanged file on its `(mtime, size)`
  fingerprint *before* any watermark is read; the fingerprint cache belongs to the
  running daemon, which flushes its own copy back over anything a repair clears. The
  watermark is now rewound to line 0 rather than deleted, and the ledgered files are
  read directly rather than polled for. Nothing was asserting the end-to-end
  property — that content the old parser skipped is in the archive afterwards — so
  the tests now do.

- **`/retrieval` reports latency per front door, and the viewer is one of them.** The
  page led with one median across every surface — and the surfaces differ by more than
  any change it was built to catch: over 14 days a question costs 849ms through the
  MCP tools, 501ms in the viewer, and a terminal search is cold on 15 of its 18 calls
  at 7.5s. The pooled 682ms is nobody's experience. Every headline number — typical
  search, slow 1 in 10, bulk & paged, first search after a restart, process starts — is
  now a column per door, with the pooled row kept last and named as the mixture it is.
  The viewer's searches come from `web-requests.jsonl`, which stays a separate ledger
  so evals mined from observed traffic never learn from a human clicking around; a
  latency page is not that question, and the two record the same fields on purpose.
  Those rows now also carry the shape of the ask (`limit`/`page`, never the query
  text): the viewer paints a fixed 40-row page, so without it every browse of a result
  set read as a question that took a second. The interactive/bulk boundary is a door's
  own first screen for the same reason — 40 rows is a sweep from an agent that defaults
  to 10 and is one question from a viewer that cannot show fewer. A door that warms and
  serves nothing gets a row too: three processes paying 31s of model load for traffic
  that never came is invisible in every other number on the page.
- **The two new caches say what they did, instead of leaving it to be inferred from a
  zero.** The probe's rule is that an absent field means *did not happen* and an
  explicit zero means *measured and instant*, and both caches broke it: a query vector
  served from the embedder's cache reports `embed_ms: 0.0` on a vector arm that ran,
  which is byte-identical to an arm that never embedded, and a `set_ms` of 20ms is a
  working memo on this corpus and a defeated one on a larger. Neither is answerable
  from a duration. `embed_cached` now rides the record when a vector was reused, and
  `SET_OUTCOMES` (`set_scans` / `set_deltas` / `set_hits`) tallies what the exact-set
  stage actually did — counters rather than one state, because a search resolves the
  set twice (the thread tally and the saturated-pool count) and the two need not agree.
  Measured live end to end: 1223.8ms `set_scans` → 0.5ms `set_hits` → 55.8ms
  `set_deltas` after five rows landed. The memo regression this release fixes was
  invisible in every field the ledger had; it is now one column.
- **Ingest no longer throws away the exact-set memo mid-walk: 852ms → 19ms per page.**
  The memo keyed on the FTS append watermark, which moves on every indexed event — and
  with the watcher writing every few seconds (`wal_age_s` p50 7.1s), a browse walk of
  any length spanned several ingests and re-resolved the whole match set per page. The
  usage ledger caught both sides of it: one 38-page walk hit 0% and spent 246.6s in set
  scans, the 46-page walk beside it hit 46% and spent 41s — same query, decided by
  whether ingest happened to be running. The watermark now rides in the memo *value*
  rather than the key, so an entry stays findable after the index moves, and
  `matched_threads` scans only the rows above the stored watermark and folds them in.
  The merge is exact rather than approximate because the two scans partition the rows
  they aggregate — which is also why the base scan is now bounded at the watermark it
  is stored under (`rowid <= :set_ceiling`); ingest runs while a scan does, and without
  the ceiling a row appended mid-scan landed in the base *and* in the next delta, which
  double-counted it into the tally. A set truncated by `SET_SCAN_CAP` is rescanned
  rather than extended: it holds the newest `set_cap` matched rows and nothing else, so
  folding a delta in makes a tally that grows past its own cap with every page instead
  of the fixed floor the cap defines. Measured over this corpus, verified equal to a
  single full scan across five query shapes under live ingest.
- **A repeated query embeds once. 326ms → 0ms.** `embed_query` ran a forward pass every
  call, and the same text embeds to the same vector for as long as one model is loaded
  — so paging re-embedded one identical string per page, and a caller comparing filters
  re-embedded per variant. Over the usage ledger, 43.9s of 177s of embed time was text
  already embedded earlier in the same file (`auth flow` 5× for 31.7s, `archive mcp
  server` 14× for 24.4s). Now memoized per embedder, keyed by `(space, capped text)` so
  a process whose configured model changes cannot be served a vector from the space it
  left. Only successful embeds are cached — `None` is the degrade path and every way of
  reaching it is a condition that resolves.
- **Concurrent callers share one disk walk instead of racing for the same disk.** The
  walk is ~0.3s at rest and tens of seconds when two overlap, and a polled endpoint is
  a machine for producing that overlap — the web ledger caught the two worst walks
  (142.9s and 42.1s) starting in the same second. `disk_usage` now takes a staleness
  budget the caller states: the default measures, so an operator who just reclaimed
  space is never told the old number, and only `/api/disk` opts in. Overlapping callers
  wait for the walk in flight regardless of budget, which costs no freshness because a
  result that lands while a caller is queued is still newer than the moment it asked.
  The sharing itself is `_ops/shared_work.SharedWork`, so the concurrency contract is
  driven directly rather than through the caller that needs it.
- **The wheel stops shipping one archive's damage history.** `_scripts/` held eleven
  one-shot repairs — ~3.5k LOC of module plus ~4.7k LOC of test — each written to undo a
  specific bug this repo's own importers once had on the maintainer's store: NULL and
  thread-id-prefixed dedup keys, the `codex` model placeholder, grok tool names lost to a
  chunk boundary, six backfills of fields older importers dropped. Every one of them
  shipped to every installer, was type-checked, coverage-floored at 97%, and maintained
  against a moving codebase — for damage a fresh install does not have and cannot acquire,
  behind private module paths no user could discover or run. They rot in place, too:
  `denamespace_dedup_keys` matched its targets against `Event.thread_id`, so the ULID
  migration silently turned it into a no-op that finds nothing while 393k prefixed keys sit
  in the store it was written for. A repair whose subject is one machine's past belongs in
  git history, which keeps it exactly as well and ships it to nobody. The v1→v2 ULID truth
  migration is the one that isn't damage — it is the documented upgrade path for any
  archive written before format v2 (`thread-archive index migrate`), so it moves to
  `_truth/migrate_v2.py` and lives beside the format it migrates, as product surface rather
  than a script.
- **One warm pass at a time per machine, and the queue is recorded.** Warming loads a
  torch model, reads the vector pack end to end and pulls the graph off disk, and none
  of it is shareable — the model has to end up resident in the process doing the
  warming, so unlike the corpus graph these passes cannot be deduplicated, only kept
  from thrashing each other. They overlapped by default: several services warm
  independently and restarts arrive in bursts, and the ledger caught two passes 1.9s
  apart taking ~303s each against ~4s for one with the box to itself, because
  concurrent passes evict each other's page cache and contend for the accelerator.
  `warm_models` now takes a turn on an flock over `<home>/.warm.lock`. Three processes
  started in the same instant serialize to 5.3s / 9.2s / 13.1s — and the queued ones
  spend *less* time working than the first (`search_ms` 2.0s → 0.34s) because a
  serialized pass inherits the page cache its predecessor warmed. Waiting is safe
  because a warm pass is off the request path by construction; every failure mode ends
  in a warmed process instead — flock releases on death, a wedged holder times out
  after 120s and warms unserialized, and a lock that cannot be opened at all is skipped
  rather than waited on. `wait_ms` rides in the warm record beside the stages, because
  a slow restart that was slow *work* and one that was a slow *turn* want opposite
  fixes and are indistinguishable in a total.
- **A starting process loads the corpus graph instead of rebuilding it: 8.9s → 0.12s.**
  `graph_cache` has persisted the graph for exactly this, but the warm pass asked for
  it through `get(block=True)`, which routes to `build()` — the authoritative path,
  which requires the current validity token. Under continuous ingest that token moves
  every few minutes, so a restart never matched and every warm pass paid a full
  corpus-wide Leiden partition; the usage ledger's `graph_ms` (p50 8.88s, p90 19.8s)
  was the documented *build* cost, not a load. `embed_graph.warm()` is now the
  starting-server door and serves the persisted partition stale, exactly as the search
  path already does; `get(block=True)` keeps its strict semantics for the eval, which
  must be a function of its snapshot and not of what a previous process left on disk.
  Measured on this archive: 177 process starts over six days spent 2,615s here.
- **The machine rebuilds the graph at most once per `rebuild_floor_s`, not once per
  process.** `_REFRESH_COOLDOWN_S` bounded how often one process re-probes, but nothing
  bounded the fleet: restarts arrive in bursts, every fresh process finds a token ingest
  has moved, and each independently rebuilds the same partition — the ledger caught two
  warms 1.9s apart that took 303s each, contending for the box over identical work. A
  rebuild is now skipped when any process persisted a graph within the floor (15 min by
  default, `THREAD_ARCHIVE_GRAPH_REBUILD_FLOOR_S`), which is two orders of magnitude
  inside the week of staleness `graph_cache.max_age_s` already accepts for a community
  prior. Never gated when there is nothing on disk: the floor suppresses duplicate work,
  never the only copy of it.
- **The embed drain's pending-doc select is bounded by the batch, not by the corpus.**
  It read as one `GROUP BY` over all of `event_vectors` joined against the whole FTS
  shadow, with temp b-trees for both the grouping and the ordering — so SQLite
  materialized 272k aggregate rows and sorted 242k candidates to return 64, on every
  poll, growing with the corpus rather than with the backlog. `select_ms` was 70% of
  embed-pass time (446s of 635s, p50 1.7s, worst 43s) against 30% for the encode that
  is the actual work. The count now comes from a correlated primary-key probe, and
  `ORDER BY` names `content_type` beside `event_id` so the sort matches the group key
  column for column — which is what lets one walk of the new `idx_events_fts_pending`
  answer both and stop at the `LIMIT`. A backlog pass drops from 302ms to 0.1ms; a
  caught-up pass, which must still prove nothing is pending, from 361ms to 318ms. Rows
  returned are byte-identical, verified against the old query over the live corpus.
- **One definition of "probe query".** `latency_replay` and `retrieval_report` each
  carried their own list of the throwaway text a bench leaves in the usage ledger, and
  they had drifted — so two reports over one file disagreed about which rows counted as
  traffic. `usage.PROBE_QUERIES` is now the single list, beside the ledger it describes.

- **The viewer has a developer telemetry page.** Web endpoint latency and errors,
  ingest throughput and stage cost, retained ingest-fault signatures, and the
  operational ledger inventory are readable together at `/telemetry`; the route
  and navigation exist only when developer panels are enabled.
- **PerLTQA's standing measurement is a deterministic 2,000-question sample.**
  The complete 8,588-question set costs about 44 minutes across its lexical and
  vector arms; the hash sample preserves coverage across people and memory types,
  resolves deltas to 0.0005, and puts the pair near 10 minutes. Both rows carry
  `~2000` in their names so their ledger history cannot mix with a different
  query set.
- **The CLI is public surface, and the docs finally say so.** Every verb is a supported interface, not just
  `search` and `read` — the service manifests, cron entries, operator scripts and shell histories that name them
  cannot follow a rename, which is why nothing here has ever renamed a verb without leaving the old spelling
  resolving (`_LEGACY_VERBS`). The docs described that seam as private operational tooling anyway, and
  `tests/test_public_api.py` pinned the tree while explicitly disclaiming it as "not a compatibility promise to
  anyone external." `docs/stability.md` now lists **five** public interfaces, with the line drawn at what a verb
  is called and what flags it takes — what a verb *prints* is still free to change, except for `search` / `read`,
  whose output is contract because it is what `archive-mcp` serves. Also `source ingest`, which the CLI has and
  `docs/cli.md` didn't list, and the durability verbs in `docs/format.md`, which were still spelled
  `verify` / `reindex` / `restore-drill` from before the noun groups.
- **The bench runs again: every harness still passed the `group=` that 0.0.9 removed.** Dropping thread grouping
  from `search` left all five lab harnesses (`beir_eval`, `cdr_eval`, `haystack_eval`, `mtrag_eval`,
  `perltqa_eval`) calling `api.search(..., group="none")`, so every scored row died on `TypeError` — the whole
  bench, not one dataset. `group='none'` asked for exactly what search now always does, so the argument is gone
  and the comments explaining it state the behavior instead.
- **BEAM is scored as the retrieval benchmark it partly is: one tier, seven categories.** Only the **100K tier**
  is carried — the 500K and 1M tiers are the same 20 conversations extended, so the length ladder they buy costs
  ~11 hours of embed to re-ask questions 100K already asks (their parquets and built homes are deleted). And
  three of the ten categories are skipped as not-retrieval-questions, listed with their reasons in
  `haystack_eval.BEAM_UNSCORED`: `abstention` (no gold by design, already skipped), `summarization` (matches the
  whole corpus by construction, and gold up to 16 messages caps a *perfect* retriever at recall@10 = 0.625), and
  `event_ordering` (asks for a sequencing over a broad topic, with no distinguishing content to match on).
  `instruction_following` scores 0.163 and is deliberately kept: finding the message that answers a broad
  question is retrieval doing its job, and dropping a row for being hard is how a bench stops measuring anything.
  354 queries → 279, and the bench set goes 16 rows/~412 min to 12 rows/~98 min.
- **Real conversations never land in the checkout.** Two paths put private transcript payloads inside the repo,
  both reachable only on a maintainer's box. `repair_grok_tool_names` resolved its plan and undo dumps from
  `Path(__file__).parents[3]` — the checkout root, which is not even a real directory once the package is installed
  from a wheel — so it now resolves them against the archive home (`repair-dumps/`, beside the store it patches,
  the same shape `migrate_thread_ulids` already used for `pre-ulid-backup`). And `obfuscate_fixtures.py`, which
  scrubbed your live `~/.claude`/`~/.codex`/`~/.grok`/opencode stores into a gitignored corpus under
  `tests/install/`, is deleted along with the mount plumbing that served it: obfuscation was lossy but never a
  guarantee, and the install lanes read the committed synthetic corpus. Real upstream shapes still reach the
  importers — through the `source fix` scaffold's `samples/`, which is already under the home. The package-tree
  ratchet keeps its git half, now on dump-marked filenames anywhere rather than one blessed directory.

- **A benchmark corpus's embed survives losing its index.** `api.embed` writes vectors into `index.db`, the
  disposable half of an archive home, and the lab's five build paths stopped there — so a rebuild, an index-format
  migration, or a `--rebuild` pass threw away hours. `eval_home.embed_corpus` now embeds and then writes
  `truth/vectors.sqlite`, the durable cache keyed by the event ids truth fixes; every harness (`beir_eval`,
  `cdr_eval`, `perltqa_eval`, `haystack_eval`, `haystack_corpus`) goes through it. The save stays in the lab rather
  than inside `api.embed` because the sidecar is rewritten whole: proportional after a one-shot corpus build, not
  after a live archive's incremental drain. Fail-soft — the vectors are in the index either way. Backfilled across
  the 30 already-embedded homes on this box (1.40 GB), and verified end to end: a real beam corpus with `index.db`
  deleted rebuilt to `vectors_restored 309` with the embedder switched off.

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
