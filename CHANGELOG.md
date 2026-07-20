# Changelog

## Unreleased

- The embedding and re-rank models became injectable collaborators.
  `embed.Embedder` and `rerank.Reranker` each own one model and the policy for
  querying it (prefixes and char cap; doc-head cap and batch size), constructed
  around a model that is already loaded (`model=`) or a loader of the caller's
  own (`load=`) — the module-level functions now delegate to a process default
  (`embed.default()` / `rerank.default()`) instead of reaching into a shared
  mutable `SLOT`, which is gone. `vectors.search`, `vectors.index_events_local`,
  `_retrieval.search` and `warm_models` take `embedder=` / `reranker=`
  arguments, so a host that already holds a SentenceTransformer lends it rather
  than paying for a second copy, and a re-embed can index into a second
  embedding space while the default keeps serving queries.
- Both model arms have an operator off switch: `THREAD_ARCHIVE_EMBED=off` and
  `THREAD_ARCHIVE_RERANK=off` (also `0`/`false`/`no`) report the arm
  unavailable, pinning a process to lexical search without uninstalling the
  `[embeddings]` extra. `retrieval_eval.py --lexical-only` sets them instead of
  reassigning `is_available` on the modules at runtime.
- The configured model names and revision pin are read per call rather than
  frozen at import, so `THREAD_ARCHIVE_EMBED_MODEL` /
  `THREAD_ARCHIVE_RERANK_MODEL` / `THREAD_ARCHIVE_EMBED_REVISION` are honored
  whenever they are set. Revision pins are per model (`embed.PINNED_REVISIONS`).
- Fixed: a model exposing only sentence-transformers' newer
  `get_embedding_dimension` accessor crashed the embedding load — the fallback
  to `get_sentence_embedding_dimension` was evaluated eagerly. The dimension is
  a log detail, so a model carrying neither accessor now loads too.
- `retrieval_eval.evaluate` takes the ranker under test as `search=`, so a
  candidate ranking can be scored against the same cases as the incumbent.

- `repair_grok_tool_names` stamps its undo dump when it writes it. The backup
  path was a module constant built from `datetime.now()` at *import*, so a
  long-lived process (or a second run in the same interpreter) wrote every
  dump under the first run's timestamp. It is now `backup_path_for(plan)`,
  resolved at write time and landing beside the plan it read, and the script
  took the `_scripts` house shape: `run(*, apply, plan_path, backup_path)` plus
  a `main(argv)` argparse adapter, in place of scanning `sys.argv` for
  `--apply` and reading two module globals. `migrate_thread_ulids.main` takes
  `argv` for the same reason.

- Each CLI verb's operator report is its own function. `cmd_backup` /
  `cmd_verify` / `cmd_restore_drill` / `cmd_restore` / `cmd_nightly` /
  `cmd_repair` / `cmd_redact` / `cmd_status` / `cmd_coverage` / `cmd_mirror` /
  `cmd_self_update` now call the `_api` layer and hand the result to a matching
  `report_*(res) -> int`, which prints the report and returns the exit code.
  The verbs' output and exit codes are unchanged; the split separates running
  an operation from rendering its result, so every warning, sample, and failure
  line can be driven from the result shape that produces it.

- The setup flow takes the host as a collaborator. `_setup/machine.py`'s
  `Machine` holds every question setup asks the machine (is this macOS, is the
  watcher / nightly backup / curation pair already scheduled for this home,
  does this install have the curation and embeddings packages) and both changes
  it makes to it (install the watcher, install the nightly backup);
  `run_setup` and `print_status` take one through a `machine=` parameter and
  default to the real host. The module-level `watcher_running` /
  `backup_running` / `curation_running` / `curation_package_present` /
  `embeddings_installed` probes moved onto it, and `run_setup`'s four
  `offer_*` override parameters are gone — one seam replaces them. `_ask` /
  `_ask_path` take the prompt reader (`read=`, default `input`), and `run_setup`
  now threads its `ask` down into the watcher and MCP steps, so a scripted run
  answers those prompts too instead of silently taking their defaults.

- Finished the librarian split through the read surface. The graph analytics
  (`_knowledge/graph.py`, `_knowledge/_community.py`), the relevant-subjects
  search lens (`_retrieval/subjects.py`), and the corpus embedding graph
  (`_retrieval/embed_graph.py` + `scripts/graph_eval.py`) moved to
  thread-librarian, taking networkx/scipy/leidenalg/python-igraph out of the
  base dependencies. The archive keeps the knowledge layer's data plane
  (KgEvent truth + fold, projections, SQL topic reads) as an unadvertised
  compatibility surface: `thread_read(topic_id)` still renders a topic's page
  (graph metadata and peers appear only when thread-librarian is installed,
  via fail-soft seams) and `thread_search(topic_id=…)` still scopes.
  `thread_read('topics')` now points at the librarian MCP's new `topic_tree`
  tool instead of rendering the tree; the MCP docstrings stopped advertising
  the topic features; the web viewer dropped its topic pages
  (`/api/topics`, `/api/topics/tree`, `/api/topic/<id>`, the TopicsView /
  TopicView routes) and `api.knowledge_status` / `bridge_topics` /
  `topic_peers` moved behind librarian's own `get_status` /
  `get_bridge_topics` / `get_community_peers`. Rationale: 8,189 topics
  existed but a 1,411-read usage window showed one topic-tree read and no
  distinct topic reads — uptake, not curation volume, is what distinguishes a
  useful lens from a terrarium.
- `fix-import` no longer spawns an agent. `archive fix-import <provider>` scaffolds and stops: the scaffold now carries
  the repair protocol as `PROTOCOL.md` beside the evidence, samples, and quirks, and the fix is written by the user or
  by whatever agent they point at the directory. Dropped with the spawn: `--scaffold-only` (scaffolding is the default
  action now), `--timeout`, and the `repair.model` / `repair.effort` config keys. Activation is unchanged and remains
  the only path a patch has to going live.
- Onboarding-trust fixes from the pre-release product review (2026-07-20). Setup no longer connects an agent to the
  wrong archive: MCP detection reads `claude mcp get`'s scope/approval status and the entry's `THREAD_ARCHIVE_HOME`
  instead of accepting any server with a matching name, and both `claude mcp add` and the printed config block pin the
  home when it isn't the default. `_agent_covers_home` compares the launchd plist's home against the *literal* default
  rather than the process env — `open_archive` pins the selected home into `$THREAD_ARCHIVE_HOME`, which made a custom
  home's status claim the default home's watcher, backup, and curation schedules. The watcher offer now discloses that
  installing it enables daily self-update from release tags, and names the config opt-out. The viewer hides its curation
  page and nav entry when the optional curation package isn't installed (`/api/status` reports `curation_available`),
  and the page itself says so instead of erroring. Install docs install the `dev` extra before invoking pytest (it does
  not ship in the base install), and the provider counts/lists in README and claude-install.md match the registry.
- Public-release hardening sweep (2026-07-20). The `fix-import` repair spawn drops `bypassPermissions` for a scoped
  surface: `acceptEdits` bounded to the scaffold cwd, a Bash allowlist of the protocol's commands (the venv's bin dir
  now leads the spawn's PATH so bare names resolve), no injected MCP servers; docs state the posture honestly.
  GitHub CI no longer installs the private thread-librarian sibling — nothing may require it anywhere — and
  `coverage_gate.py` carries a second, measured floor set for librarian-free runs, so fork PRs pass without secrets.
  Self-update's truth-format probe anchors on `_truth/layout.py` (whole-tag grep only as fallback) so a stray
  assignment can't shadow the gate. New SECURITY.md (trust model, self-update supply chain, vuln reporting) and
  CONTRIBUTING.md; releasing.md gains standing repo-hardening requirements (2FA, tag/branch protection).
  Personal references scrubbed from docstrings and test fixtures; README repairs (broken Install sentence,
  "dependency-free" wording, platform story, fix-import claims, third-party notices for the bundled viewer via
  `scripts/gen_third_party_notices.py`); the stray root `node_modules/` vitest artifact is untracked and ignored.
  thread-librarian disappears from the public surfaces entirely — docs describe curation generically, and the setup
  wizard/status only mention the curation package's commands when it is actually importable on the machine.

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
