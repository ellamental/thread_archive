# Changelog

## Unreleased

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
