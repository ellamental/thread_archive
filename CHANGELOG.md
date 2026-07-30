# Changelog

## Unreleased

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
