# Changelog

## Unreleased

- A drifted corpus could verify as its own pin, on exactly the filesystems the install lane runs on.
  `dataset_pins.file_digest` memoizes each file's sha256 on `(size, mtime_ns)`, which only detects a
  change where the bytes moving are guaranteed to move that key — and timestamps are too coarse for
  that on a freshly written file: Linux stamps mtime from a jiffy-granular clock, so a same-length
  rewrite landing in the same tick as the read that hashed the file moves nothing the memo looks at.
  Entries now also record when they were read, and are believed only where the file's mtime predates
  that read by two seconds (`SETTLE_NS`, clearing every timestamp granularity in use); anything more
  recent is re-read, which costs a real corpus nothing, since the settled files are the gigabytes.
  Entries written before this are re-hashed once. The two tests that caught it only red on a coarse
  clock — green on APFS, red in the Linux container — so a third pins the case everywhere by forcing
  the shared tick with `utime`.

- The release machinery gets a weekly drill, so it breaks on a quiet Tuesday instead of mid-ship.
  `.github/workflows/drill.yml` (weekly cron; `scripts/release_drill.sh` dispatches it by hand) builds
  dev's tip under throwaway `999.run.N` versions, publishes an rc and a final to TestPyPI over the
  Trusted Publishing path, and drives the operator surface against what the index serves — rc opt-in
  semantics, a fresh install running the real ingest lifecycle, and `self-update` exercised end to end
  (upgrade, the rc-never-offered guardrail, forced rollback) via the new
  `tests/install/self_update_check.py`. The GitHub-settings audit rides the same run as its own job
  (via the read-only `RULESET_AUDIT_TOKEN` — bypass actors are invisible below admin) and now also
  checks the `pypi`/`testpypi` environments exist. The drill mints no tags, never touches `main` or
  PyPI, and takes only its own artifact from TestPyPI (`--no-deps` everywhere — an open index never
  serves dependencies). The whole workflow gates on the `DRILL_ENABLED` repository variable (off until
  the TestPyPI publisher and audit token exist), so it ships armed but silent.

- Claude Code's ledgers catch up with the format, and Claude Science stops being blamed for it.
  Three shapes the live store carries went undeclared: the `fallback` block a safeguard-flagged turn
  leaves when it is re-run on another model (the read path has always rendered it as a model switch —
  only the ledger hadn't been told), and two assistant-line fields, `isAbortedMidStream` and a
  snake_case `session_id` twin of `sessionId`. Separately, the Claude Science importer ran its
  validation under `claude-code`: the app calls Anthropic's server-side tools, so every web search it
  ran filed a `server_tool_use` / `web_search_tool_result` finding against Claude Code's ledger — drift
  Claude Code hadn't grown, pointing a fix at the wrong parser and, past the threshold, degrading the
  wrong source. It now validates under its own source against its own `ProviderConfig`, where those two
  block types are declared. Claude Code's own ledger stays blind to them, so it still surfaces the day
  Claude Code starts emitting them. Both live stores re-validate with no findings.

- Claude Science messages import on the app's own clock, and the store's record can no longer grow
  a key in silence. `frame_messages` rows carry `_ts` (epoch ms) on newer app versions; the importer
  had never read it, so every message landed on the synthetic one-second ladder built for frames that
  have no timestamps at all — one 127-message session, 52 minutes of work, imported as 2 minutes.
  `_ts` now wins where it exists and the ladder stays as the fallback. `_refusal`, `_intent_id` and
  `_async_exec` (a background run's exec id and interrupted flag) join the annotations the import
  carries; `_has_server_tools` is a documented drop, since the turn's own server-tool blocks already
  say it. The reason a real timestamp sat unread for a month is that `msg_json` is not a source *line*,
  so the parser's field-level drift ledger structurally cannot see it: every key the importer accounts
  for is now named in one set, and anything outside it is preserved under the message's
  `annotations["unmodeled"]` and recorded to the drift ledger — the same bargain the line-based sources
  get. Events already imported keep the timestamps they were written with; the correction is forward-only.

- The release process closes the seams a post-0.0.14 audit found. `release_finish.sh` no longer
  settles for the tag existing: it finds the tag's Publish run and blocks until the whole workflow —
  the `verify` job included — is green (0.0.12 and 0.0.13 both finished while verification was still
  running), and it refuses to remove the release worktree while any process is still running from it
  (a mid-sweep removal was one source of phantom-red local CI). CI's `push: release/**` trigger is
  gone — release_cut.sh opens the release PR at cut time, so every release-branch push already runs
  as a `pull_request` event, and the branch-push run was a ~25-runner-minute duplicate that could
  never cancel against it. Bench's actions are SHA-pinned like the other credentialed workflows (it
  holds the packs token), and Dependabot now also covers both npm lockfile trees. The install lane
  pre-pulls its base image with retries and settles for a local copy when Docker Hub is down —
  registry timeouts were the dominant cause of red install rows in local sweeps.

- The nightly restore drill goes weekly, on the same age gate the deep verify rides
  (`_DRILL_EVERY_DAYS = 7`) — and stays nightly for as long as it is failing, because `_health_is_due`
  reads a not-ok record as due. That keeps the property that made it nightly (the restore path is code;
  a regression in it surfaces the next morning and keeps surfacing) while a healthy restore path stops
  spending the night on it. The drill is the one stage whose cost tracks the whole corpus — a full index
  rebuild from the mirror, 66 of the night's 80 minutes — so it is what would have grown the window past
  the morning. Age-gated rather than calendar-gated: a machine that was off on the due day drills on its
  next nightly. `--no-drill` still withholds the stage outright, and `backup drill` still forces one.
  A skipped night reports `escalations.drill = False` and carries no `drill` result — absent, not failed.

- Substring search rides an index. A second external-content FTS5 over the same `events_fts` shadow, tokenized
  into trigrams (`event_substr`), turns an infix `LIKE` from a pass over the corpus into a rowid prefilter the
  escaped `LIKE` then verifies. Measured over 120k docs: a substring that matches nothing — the scan's worst
  case, since no `LIMIT` can stop a walk that never finds a row — goes 36ms → 0.1ms, and ordinary identifier
  queries 30–40x. The prefilter carries no `ESCAPE` clause (one turns fts5's LIKE optimization off outright), so
  `%` and `_` degrade there from literals to wildcards — a strictly wider candidate set, which is what keeps the
  pair exact; a hypothesis property asserts the two forms agree on every query. Costs ~1GB on a 0.36GB corpus.
- The prefilter is taken only where it pays, decided per query against the index rather than from the term's
  text. It rides on a rowid subquery, which is materialized before the outer `LIMIT` can stop — so for a term
  matching a large slice of the corpus it forfeits the early-out that makes the plain scan bearable and then
  adds a per-candidate verify, measured 2.9x *slower* for a term in 7% of the corpus. A bounded probe (2000
  candidates) settles it: `thread_id` and `session` take the scan, `getattr` and `SET_EXAMINE_CAP` the index,
  and the probe costs ~8% of the scan it declines against a few ms on the 30–60x it buys. Selectivity is not
  readable from the query — `SET_EXAMINE_CAP` and `thread_id` are the same shape and differ ten-thousandfold in
  what they match. Terms with no 3-character literal run (`p4`) never reach the probe.
- An indexed substring set is no longer bounded by `SET_EXAMINE_CAP`. The window exists to stop a *scanning*
  predicate from costing more as the corpus grows, and a prefiltered substring query does not scan — so its
  counts and thread enumerations stay exact instead of degrading to a floor over the newest 2M rowids. That
  bound was the nearer of the two: the shadow's rowid high-water mark was at 1.27M of it.
- `thread-archive index substring` builds the trigram index from the shadow already on disk — the targeted heal
  for an archive predating it, where `index rebuild` would re-derive the whole shadow from the events. One
  transaction, so ingest waits rather than interleaving a row into the shadow that the index would never see.
  `verify` reports the index's row count and whether it is complete, deliberately outside `ok`: an archive
  without it has lost no data and no correctness, only the index, and retrieval probes for it and falls back.
- The sync triggers are replaced when their stored body differs from the one the module defines, rather than
  only when absent. A trigger that predates a change to the set still fires, so a presence check would pass it
  while it mirrored a shadow write to only some of the indexes.

- The Bench workflow keeps what each pass cost: a non-gating `perf-trend-*` artifact per CI run
  (`python -m search_lab perf`) carrying every row's wall clock off the run ledger, normalized by two synthetic
  hardware calibrators (BLAS matvec for the vector arm, FTS5 scan for the lexical) so numbers from different
  runners compare. Measured across two same-day runners the machine-speed factor was ~14% with arm-shaped
  residue — the artifact series is how the real variance envelope gets characterized before any band is set.

- PyPI is the only supported install. `git+<repo-url>@vX.Y.Z` stops being a distribution channel and the manual
  drops its from-source install: a checkout is a development environment — the maintainer's or a fork's — and its
  recipe lives in `CONTRIBUTING.md`, while `scope.md` states the policy beside the other deliberate limits.
  `pip`, `uv tool` and `pipx` resolve the same wheel from the same index, and self-update still refuses a checkout.

- The Publish `verify` job proves the published wheel *works*, not just that it starts: after the entry-point smoke it
  checks out the tagged tree for `tests/install/e2e_check.py` and runs a real lifecycle on the installed artifact —
  a corpus for every provider, imported through each provider's own importer, the index rebuilt from the JSONL truth,
  every marker searched back out. Seconds, and it covers the one surface nothing else did: the published artifact
  under dependencies resolved fresh from the index (the `package` and install lanes both build and resolve locally).
  A guard asserts `thread_archive` resolves under site-packages, so the lane can never quietly re-test the checkout.
  This is what the mandatory rc's `verify` has always claimed to establish; until now it established two `--help`s.

- The import-parsing layer sheds dead surface. `thread_archive.provider.parse` no longer re-exports
  `normalize_tool_name` (its module — with `TOOL_NAME_MAP`, `FILE_TOOLS`, `PROVIDER_PATH_FIELDS` — had no callers
  and is gone), `parsers` no longer re-exports a `ValidationSeverity` that duplicated the live one in
  `parsers.validators.base`, and `BaseValidator` drops two override hooks nothing overrode. The three DB
  scanners' identical per-unit result dataclasses collapse into one `DbUnitImportResult` in `_importers._result`.

- The watcher stops writing telemetry nothing read. `health.json` loses `watch_embed_last` and
  `watch_maintain_last` entirely, and `watch_pass_last` loses `pass_ms`/`pass_ms_max`/`lag_s`; the embed and
  maintenance passes still record into the ingest ledger, which keeps a series rather than only the last pass.
  The two probe queries those numbers cost per pass (newest event, newest embedded event) are gone with them.
  `watch_pass_last`'s liveness and per-source counters — the keys `status` and the capture audit read — are
  unchanged.

- Service management is one API by agent name. `_service` drops twelve per-agent wrappers for
  `install_agent`/`agent_status` beside the existing `restart_agent`/`uninstall_agent`/`agent_installed`, with a
  spec-builder table as the only place the three agents differ; `daemon install/uninstall/restart/status` runs
  one path instead of three copies. The setup wizard's dead standalone `main`/`build_parser` entry point goes
  (the CLI's `setup` verb is the only way in), the restore and restore-drill reports become one renderer, and
  the operator-facing byte formatter lives once in `_fmt.size`.

## 0.0.14 — 2026-08-01

- CI proves both platforms: macOS lanes for the pytest bar and the wheel install, beside the Linux matrix now
  carrying 3.12–3.14, the serial suite, a devweb lane, and the Docker from-nothing install proof on release PRs.
- Releasing is machinery, not memory: a release-shape check holds the §4 contract on every PR to main, the main
  ruleset requires every green (CI, Bench, CodeQL, install) before merge, and a merge without a bump fails loudly.
- The off-box quality gate runs pre-merge on every release PR, not only weekly; the portability proof ships green.
- `scripts/audit_release_settings.py` audits the GitHub-side hardening; credentialed workflows pin actions by SHA
  (dependabot keeps pins fresh); `release_cut.sh` / `release_finish.sh` script the cut and the finish.
- `watch --web` survives a ^C during startup: the interrupt gets the same clean stop and close as one in the loop,
  and closing a viewer server whose startup failed no longer crashes on a missing attribute.

## 0.0.13 — 2026-08-01

- The quality gate runs off this box: bench corpora and built homes ship as content-hashed release assets on the
  private thread-archive-bench-data repo (`python -m search_lab packs`; hashes of record in `bench-packs.json`), and a
  scheduled GitHub lane (`bench.yml`) restores them and runs `gate --run --quick` paying only for CPU query embedding.

## 0.0.12 — 2026-07-31

- Releases publish a mandatory rc first: GitHub CI, CodeQL, and the publish path are proven before the final ships.
- Web and devweb static requests resolve via an index of the built bundle; nothing the build didn't emit can be served.
- `match='substring'` honors `OR`/`|` as a union; `since`/`until` reject unknown units; hits carry source and date.
- `thread-archive search` delegates to a live shared MCP server (~0.5s vs ~6s cold); fallback re-runs in-process.
- Import fixes: claude-science incremental slicing, cursor/opencode settled-window re-scans, cursor WAL watched.
- The open archive is an object: per-archive caches under a non-recycling identity; opening no longer writes env vars.
- The KNN matrix never builds inline in a request thread; pack builds serialize on a machine-wide lock, assemble faster.
- The shipped manual is `docs/public/` (default-closed wheel include); stale retrieval rationales audited out.
- New ratchets: retrieval surface derived from the tool signature, provider conformance kit, truth↔index property test.
- The suite runs serially; coverage floors ratchet behavior, not presentation; the CI latency gate is removed.

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
