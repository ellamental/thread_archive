# Changelog

## Unreleased

- The base dependency pins `mcp<2`: mcp 2.0 removes `mcp.server.fastmcp`, the API the server is written
  against, so an unpinned fresh install broke `archive-mcp` at import (caught by the package lane's clean-venv
  MCP session tests). Five mypy errors that landed with the telemetry/ledger work are fixed alongside.

- Publishing to PyPI: a `v*` tag push now also uploads the wheel + sdist to PyPI via Trusted Publishing
  (`.github/workflows/publish.yml`); `pip install thread-archive` becomes a supported install path.

- **The corpus graph survives a restart, so search ranks the same on both sides of one.** The graph the coherence
  re-rank orders by was a process-local cache: every restart started empty, and until a build landed the re-rank
  stood down. The cost was never the point — the build is off the request path by construction, and a search runs
  correctly without it. What a restart actually produced was a window in which the same query came back in a
  different order, with nothing in the output saying which one you got, and the ledger puts that window at a p50
  of 8.7s and a p90 of 21.6s across 137 warm passes. `_retrieval/graph_cache.py` writes the graph beside the
  vector pack, named by the store's validity token, and a fresh process serves it on the first search: 9.6s to
  build this corpus, 0.18s to load it. When the token has not moved there is no rebuild at all. When it has —
  the common case under continuous ingest — the persisted graph is served *stale* while the refresh runs behind
  it, which is what a long-lived process already does with its in-memory copy; a thread the graph has not seen
  resolves to no community and simply takes no boost, so the re-rank degrades toward a no-op for the corpus's new
  tail rather than misordering it. `THREAD_ARCHIVE_GRAPH_CACHE_TTL_S` bounds how much tail that can be (default a
  week, ~1%/day of corpus growth here; `0` disables persistence). The build's shape is recorded and required to
  match before a graph is served — neighbor count, similarity floor, content-type scope, and the community engine,
  because Leiden and the Louvain fallback partition some regions differently and one's graph is not the other's.
  Every other way of getting it wrong (missing, truncated, hand-edited, past its bound, or built from vectors an
  in-place re-embed has since rewritten) is a miss that costs one rebuild, never a graph nobody can vouch for.

- `status` reports whether a restart has a corpus graph to rank with. Its absence is invisible from the outside —
  a process without one answers every search, just in a different order — so the report names the state rather
  than leaving it to be inferred from a latency number. Read off disk; it never builds one to find out, and stays
  quiet on an archive with nothing embedded, where there is no graph to have and none to miss.

- A disabled self-updater no longer files notices. `update.enabled: false` stops the check from ever running
  again, which froze its last verdict in the action queue permanently — this install had been showing "Updates
  are blocked — working tree not clean" since the day it was switched off, recorded against a version two
  releases back, asking the operator to act on a mechanism they had turned off. `operational_records` now
  carries `update_enabled` and `build_notices` drops both update notices when it is false; unset still means
  on, so an install that never touched the key is unaffected, and an unreadable config reads as on rather than
  as a way to go quiet. The status line and health panel still report the record — it is a fact, and only the
  judgment over it was wrong.

- `thread_search(commit=…)` resolves every file of a commit instead of the first 300. The cap existed because each
  file was its own query, and a bulk checkpoint here runs to 1500 paths — but the contributors of `e1c33c3` all
  sorted past position 300, so the commit reported *zero* contributing sessions when it had five, indistinguishable
  from a commit nobody worked on. Files sharing an authorship floor share a window and a commit's files were mostly
  last committed together (1536 files → 3 distinct floors), so the lookup now groups by floor and batches each group
  into `IN (...)`: the whole 1536-file commit costs three queries and ~100ms, against the ~200ms the floor walk
  spends in `git log` regardless. Coverage denominators mean what they say again — with a prefix resolved, no
  session on that commit could score above 0.195 however much of it they wrote.

- The backup check bounds mirror coverage on both sides. A mirror *below* the live truth is ordinary staleness, but
  the truth only grows, so one holding *more* is holding content the archive let go of — a renamed file's twin, a
  deletion the mirror never applied — and that surplus is what a restore rebuilds from: duplicate thread files under
  two names collapse into conflicting parents the reindex refuses to publish. `verify --backup` now fails past 2%
  over parity (the restore drill's coverage floor, mirrored), naming the surplus in `coverage_excess`, so the fault
  shows while the mirror can still be pruned instead of surfacing a week later as an unexplained shrink.

- **The stats page has charts, not just tables.** Four of them: conversations per month stacked by provider,
  the same series again as a share of each month (the run spans three orders of magnitude, so the volume chart
  flattens the early years into a hairline and only the mix chart makes them legible), one panel per model of
  its tokens per month on a shared scale, a histogram of how big a session gets with the median and p90 marked,
  and a weekday × hour grid of when sessions start. Each sits above the table that spells it out, and no value
  on the page is reachable only by hovering — the rhythm grid is a real table with a count in every cell, and
  the stacked charts' legend and provider table carry the identities that color alone shouldn't.
- The stats page's provider and model tables open at their first ten rows, with the tail behind a toggle — 72
  models between the charts and the bottom of the page was most of the page. Bar scales, model colors and which
  columns exist are all still derived from every row, so nothing shifts when the tail comes back.
- The stats rollup gained a time axis. `thread_activity` records each thread's first and last event, folded over
  every event type by the same cursor as the token sums, and `request_metrics` stamps each request with its
  month. `threads.inserted_at` could not serve this: it is when the archive *ingested* a conversation, so every
  provider export ever imported collapses onto its import day and three years of history reads as one spike.
  Conversations date by when the session started, tokens by when each request happened. Rebuilt once on first
  read after upgrade (`projection_version` 2), in the watcher's background prewarm.

- **A restart no longer re-reads the archive.** The first poll after every restart read and parsed every
  transcript — 1,983 files, 1.4 GB, ~3M lines — to rediscover what it already knew, at ~11 s a time and roughly
  twenty restarts a day. Two changes, both found by the new ingest instrumentation. The parse now happens only
  once the watermark says the file changed (`LazyLines`): the append proof resolves from the file's *bytes*, so
  an unchanged transcript never needs a JSON pass at all — rescan 11.4 s → 7.4 s with the parse gone entirely.
  And the per-file `(mtime_ns, size)` fingerprints now persist across restarts, which removes the rescan
  outright. What that would also have removed — a full corpus re-verify against the watermarks, which every
  restart was providing by accident — is kept deliberately: a fingerprint cache older than
  `THREAD_ARCHIVE_FINGERPRINT_TTL_S` (6 h) is ignored, forcing the same full pass on a schedule chosen for it
  rather than one set by how often the daemon bounced. Fail-safe throughout: a missing, corrupt, or stale cache
  means a full scan, and a malformed entry is dropped individually rather than trusted.
- An unchanged file no longer re-reports the previous import's dropped lines on every poll, so the watcher's
  cumulative `parse_errors` counts real capture loss rather than inflating with each restart.
- The append proof digests its prefix through a `memoryview` instead of a slice, which was copying the whole
  transcript on every poll of a file that grew.
- Ingest is instrumented per stage, the way retrieval already was. One import splits into read / parse / cursor /
  normalize / validate / build / dedup / write / fts / commit, measured from the helpers every importer shares —
  archive's own and every plugin's — so no importer signature carries a timer. The watcher records a row per
  source poll that did work to `<home>/ingest-runs.jsonl`, and `thread-archive source ingest` reads it back per
  source and per stage. Two costs that were previously indistinguishable are now separate numbers: the append
  proof re-hashes the whole file on every poll of a live session, and a source's poll time is mostly the loop
  *looking* for work (`import_ms` against `ms` in the health record says how much).
- A checkpoint reports where it went — the ingest-lock wait, snapshots, rebalance, the changed-thread backstop —
  instead of one `checkpoint_ms`. The lock wait is time the pass spent doing nothing, charged to it by whoever
  held the lock, and it had no way to show before.
- The MCP serving layer times itself against what the tool measured of itself, so dispatch and the throttled
  catch-up kick are no longer assumed free. Recorded as a `serve` row only when the overhead clears 5 ms.
- **Telemetry rotation retains.** `retrieval-usage.jsonl`, `web-requests.jsonl`, and `load-runs.jsonl` rotated by
  renaming to `.jsonl.1`, replacing any previous rotation — so a busy stretch silently deleted the older half of
  the record. They now rotate to UTC-stamped segments and keep every one, and every reader walks all of them
  (`iter_rows`), including the retrieval report and the latency replay's query population. Segment caps still
  bound what a single read has to walk. Existing `.jsonl.1` files are read as history.

- An embed drain hands the torch allocator's cached blocks back when it finishes, and so does the startup warm
  pass. Torch keeps every accelerator block it ever allocates, so the watcher's footprint was the high-water mark
  of its largest encode, held for the life of the process — and on Apple Silicon that memory is unified and dirty
  anonymous, the kind a loaded host can only relieve by swapping. Measured on a 512-doc drain: 5411 MB held, 526 MB
  of it live, 2830 MB returned. The release runs once per drain rather than per batch, and never on the query path,
  where re-acquiring per search would cost more than it frees.
- The health page's provider table dates each source by its last import (exact time on hover), so a provider that
  has gone quiet is visible without waiting for the nightly coverage audit.
- A topic-graph event that names no writer records `actor` as `unknown` rather than as one particular external
  curation tool. Archive authors none of these records and cannot infer an identity for one, and the projections
  the events fold into already defaulted this way. Stored rows keep the actor they were written with; a live index
  picks the new column default up at the next `index rebuild`.
- Search quality is gated at release: `python -m search_lab gate` holds the benchmark set to checked-in accepted
  numbers, failing a regression, an unmeasured ranking change, or a corpus that moved under a baselined row. The
  bench ledger moves out of the archive home to `~/.local/state/thread-search-lab/` — nothing it measures is the
  archive, so the whole quality stack now reads no archive state at all.
- **The README reads correctly off GitHub, and bug reports arrive with their diagnostics attached.** The five
  remaining relative links (LICENSE, SECURITY.md, and the three `docs/` pages) resolve against the repository
  rather than the rendering page, so they survive on a package index where there is no surrounding tree to
  resolve into. An issue form asks for `thread-archive status` and `thread-archive source coverage` up front —
  the two outputs that usually identify import drift on their own — plus version, install shape, platform, and
  which search arms are installed; its config routes security reports to a private advisory and leaves blank
  issues open for the design discussion CONTRIBUTING invites.

## 0.0.7 — 2026-07-27

- The command is `thread-archive`; verbs group under `source`/`index`/`backup`/`service`, old spellings still resolve.
- Retrieval reaches the shell (`search`, `read`), `web` opens the viewer, `uninstall` takes it back off a machine.
- Search's p99 falls from 57s to ~1.5s: the cross-encoder is gone and the vector pack rebuilds off the request path.
- Ranking now scores its arms rather than their ranks, and weighs match coverage; BEIR scifact nDCG@10 0.509 → 0.650.
- Search enumerates as well as finds — `page=`, real totals, `path=`/`commit=` scopes; tool output leaves the index.
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
