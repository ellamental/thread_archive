# Changelog

## Unreleased

- 2026-07-21: `archive eval` — a read-only search-quality self-checkup an operator
  can run over their own archive ("does recall hold on my data"). Three protocols,
  no external labels and nothing leaves the machine: `--titles` (default; each
  thread's own title as the query — works day one), `--from-log` (real
  thread_search→thread_read pairs mined from the tool-use trail — meaningful once
  search has been used), and `--behavior` (zero-label click/reformulate/abandon
  rates). Output is framed as a health check, not a proof — the title proxy has
  vocabulary overlap built in and the click labels are incumbent-shaped, so both
  read as findability/collapse alarms rather than precision scores. The scoring
  core moves into the package (`_eval.py`) so the shipped command and the `evals/`
  dev bench score off one code path; the bench's deeper tiers (LLM judges,
  experiment arena, BEIR) stay dev-only.

- 2026-07-21: Linux (Ubuntu) service support. The launchd-only daemon layer
  becomes a modular service-backend registry (`_service/`): a platform-neutral
  `AgentSpec` (`_service/spec.py`) that each backend renders — launchd on macOS
  (`_service/launchd.py`, refactored from the old `_launchd.py`, plists
  unchanged), systemd `--user` on Linux (`_service/systemd.py`: `.service` units
  + a backup `.timer`, `StandardOutput=append:` logs, linger). Backends
  `register()` behind a `ServiceBackend` protocol and a resolver picks the one
  that fits the host; `cli`/`_setup.machine`/`_update` go through the front, never
  a concrete backend. `Machine.macos` → `Machine.can_schedule` (+ `service_kind`).
  Provider store paths gain Linux branches (cowork sessions, VS Code exthost
  logs). A real-systemd integration lane runs in CI (`.github/workflows/ci.yml`
  `systemd` job); the render + `systemctl`-stub tests run everywhere. Adding a
  platform (Windows) is now a drop-in backend. See `claude-install-ubuntu.md`.

- 2026-07-21: The archive no longer knows thread-librarian exists. The relevant-subjects
  search lens moves in-tree (`_retrieval/subjects.py` — it was always a pure projection
  over the archive's own `topic_messages`/`threads`), so search headers and the viewer
  keep naming subjects with zero external imports. Deleted: the graph-authority rank
  prior (`graph_prior.py`, `THREAD_ARCHIVE_GRAPH_RANK` — eval-negative, off by default),
  `topic_get`'s graph/peers enrichment (slots remain, always `None`/`[]`),
  `curation_stats` + `/api/curation` + the viewer's curation page and nav,
  `curation_available` from `/api/status`, the wizard/status curation lines and
  `Machine.curation_running`/`curation_installed`, and reindex's librarian cache reset.
  Tests seed curated data through the archive's own truth primitives
  (`tests/kg_seed.py`) instead of librarian's write surface; the coverage gate drops
  its dual with/without-librarian floors. Librarian keeps working *on top of* the
  archive (its own MCP, daemons, plugin) — the dependency now points one way only.

- The search lab gets a home: every search-quality harness
  (`retrieval_eval.py`, `retrieval_judge.py`, `retrieval_mine_gold.py`,
  `search_lab.py`, `search_arena.py`, `graph_eval.py`, `beir_eval.py`) and
  the `experiments/` directory moved from `scripts/` and the repo root into
  `evals/`, with `evals/README.md` as the lab manual (ladder, instruments,
  the change-ranking workflow). `scripts/` is repo tooling again (coverage
  gate, frontend-build check, license notices); the CI retrieval-gate row,
  tests, and docs point at the new paths. No behavior change.

- Agent-mined gold labels: `scripts/retrieval_mine_gold.py` spawns one
  headless `claude` agent per sampled real query; the agent reads the
  originating session for intent, sweeps the corpus with its own searches
  (date-bounded to the corpus as of the original search via the script's
  `tool` mode), reads candidates, and emits a corpus-grounded gold case.
  Cases append to `~/.thread/archive/judged-cases.jsonl` in the eval's
  `--cases` format with a per-case `until` bound that
  `retrieval_eval.py --cases` now passes into the scoring search — mined
  labels are deterministic under corpus growth and cost tokens once, not per
  eval run. `tests/test_retrieval_mine_gold.py` covers verdict parsing, gold
  validation (date bound, session exclusion, ref resolution), the prompt's
  baked-in bound, re-run dedupe, and the `until` flow through `evaluate`.

- New promotion instrument for retrieval experiments: `scripts/search_arena.py`
  duels a challenger configuration from `experiments/` against the shipped one
  on real mined queries — both rankings go to a headless `claude` judge, side
  order randomized per query, labels blind — and reports challenger
  wins/losses/ties with an exact two-sided sign test. Identical rankings
  short-circuit to a tie without a judge call, so token cost scales with how
  much the configurations disagree. `tests/test_search_arena.py` guards the
  blind side-attribution, the tie short-circuit, and duel scoring with a fake
  judge in the fast tier.

- The search stack is now configurable end to end, and the quality bench can
  race configurations. Every pipeline tunable — ranking weights, recency decay,
  density normalization, RRF k, pool sizes, coherence gamma — moved into one
  frozen `SearchParams` dataclass (`_retrieval/params.py`, defaults = shipped
  values with their evidence), threaded through `search(params=...)` and
  `api.search`. `experiments/` holds named configurations-as-code (a
  `SearchParams` value or a full `SEARCH` callable; contract in its README),
  and `scripts/search_lab.py` builds the synthetic quality corpus in a
  throwaway home, scores baseline + every experiment on identical cases, and
  prints an MRR/recall leaderboard with deltas — seconds lexically, `--models`
  for the fused pipeline. The corpus gained adversarial structure so
  configurations separate (a TF-spam paste bm25 favors, a recency pair whose
  old twin is the lexically stronger match), with matching tier-0 invariants;
  `tests/test_search_lab.py` keeps the params seam production-identical at
  defaults and every experiment contract-conformant.

- The web viewer's thread header now shows the Task-tool subagents a thread
  spawned: an "N agent sessions" line with a color-coded chip per model and its
  run count (e.g. `claude-haiku-4-5 ×3`, `claude-opus-4-7 ×2`), tinted to match
  the model colors elsewhere on the header. `read_thread_structured` gained an
  `agent_sessions` field that reverses the soft parent link (a subagent's
  `source_metadata.parent_session_id` + `project_dir`, plus any
  continuation-absorbed sessions via `import_state`) and tallies the runs by
  model. Threads that spawned no agents render nothing new.

- Search quality grew its fast tiers: a checked-in synthetic corpus with known
  relevance structure (`tests/quality_corpus.py`) now backs a tier-0 relevance
  eval that runs in every pytest pass (`tests/test_search_quality.py` — MRR /
  recall floors plus named ranking invariants: density, phrase contiguity,
  recency tie-break, decoy resistance, and an A/B seam that scores any
  candidate ranker on identical cases), and an opt-in `-m quality_models` lane
  runs the same corpus under the real embedding + rerank models. The README
  maps the full quality ladder — tier 0/1 synthetic, the CI retrieval gate and
  by-hand harnesses on the live archive, BEIR calibration — so a ranking
  change climbs evidence tiers instead of going straight to production
  metrics.

- The Docker install lane joined the CI manifest: an `install` row in ci.toml
  runs tests/install/run_install_test.sh on every archive commit, replacing
  the operator-run-plus-staleness-nag arrangement — the from-nothing install
  proof is invalidated by tree changes, not wall-clock, so it now re-proves
  exactly when it can break. The script acquires its own docker daemon
  (colima, started headlessly when nothing is reachable; Docker Desktop no
  longer required), and the nightly pipeline's `_install_test_alert` watcher
  and the `install_test_last` health stamp are gone — a red CI row is the
  failure signal now. The lane's first run in this shape caught real rot and
  one real bug: the image's hand-pinned test-tool list had drifted from the
  dev extra (it now installs the wheel's own `[dev]`, so the two can't
  diverge), the container needed git (product runtime: the clone is the
  install, self-update drives git) and an unprivileged user (root's
  DAC-override made every chmod-based failure-injection test a no-op), and
  `archive watch --web` shutdown called `server_close()` without
  `shutdown()` — on Linux a bare close doesn't wake the serve_forever
  poller, so the viewer port kept accepting connections after stop.

- Closed the audit's top test-suite gaps (the real reranker was never
  exercised by any automated lane; two importers sat outside the golden and
  install harnesses; two ledgers were written but never read). The CI
  retrieval-gate row now passes `--require-rerank`: a liveness probe that
  loads the real cross-encoder and scores one trivial answer/decoy pair, so a
  torch or model regression that silently kills reranking in production can no
  longer leave every row green (the metric run itself still skips per-query
  rerank). claude-science and cowork joined the provider golden suite, and the
  Docker install lane's synthetic corpus grew chatgpt/claude.ai export,
  claude-science, and cowork sessions — every packaged provider now proves
  searchable end-to-end from a clean container (the corpus's two claude-code
  sessions also stopped sharing a first message, which continuation detection
  rightly merged into one thread). The nightly pipeline gained two advisory
  watchers, warn-and-notify like the drift alert: one reads the
  retrieval-trend ledger (alerts when the gate stops writing it, or when the
  recent MRR median slides well under baseline — erosion the collapse floors
  can't see), and one ages the `install_test_last` health stamp that
  run_install_test.sh now records on a passing Docker run.

- The public plugin harness (`thread_archive.provider.testing`) is now
  dogfooded and directly tested: the internal provider goldens run through the
  shipped `assert_golden`/`write_jsonl`/`init_archive` instead of a private
  copy of the same machinery, and a dedicated suite exercises the documented
  conftest wiring end to end (isolated `archive_home`, UPDATE_GOLDENS
  write-and-skip, missing-golden and divergence refusals). Self-update's
  default executors — the real pip reinstall, `archive status` smoke,
  `archive migrate`, launchd restart, and patch retirement — gained their
  first tests, run patch-free against the venv's real binaries and throwaway
  homes. Repair coverage now includes a real activation of a scaffolded patch
  (the generated test suite refuses a fixtureless scaffold, then goes green
  with a fixture in place) plus ledger-noise, torn-manifest, and per-copy
  importer-failure recovery paths. Coverage floors ratcheted to match:
  provider 48→76, _update 70→80, _repair 82→90.

- Fixed a flaky CI hang: the watch-loop CLI tests block their main thread in
  the real `archive watch` verb and rely on a helper thread to deliver the
  interrupting SIGINT — but a readiness predicate that raised (the `--web`
  test's viewer probe hitting a not-yet-listening socket) killed that thread
  before it fired, leaving the worker blocked in the poll loop until the CI
  row's wall-clock ceiling. The helper now treats a raising predicate as
  "not ready, retry" and sends the SIGINT unconditionally from a `finally`.
  The suite also gains pytest-timeout (300s per test; the package lane
  overrides higher), so any future hang becomes a named failure with a stack
  instead of an opaque row timeout — which also stops truncated coverage
  reports from tripping phantom coverage-gate breaches.

- Truth-format boundaries now fail closed in both directions: v2 writers refuse
  to mutate a v1 directory, including the mixed integer/ULID state an interrupted
  upgrade could leave. `archive migrate` preserves and normalizes mixed trees,
  then reindexes and verifies them; an explicitly allowed self-update format bump
  runs that pipeline before restarting long-lived agents and never rolls older
  code back onto truth after migration has begun.

- The ranker carries a graph-authority prior (normalized PageRank from
  thread-librarian's curated corpus graph as a bounded, boost-only score
  multiplier), built to test whether curation authority improves retrieval.
  It ships off: on the log-mined click protocol it degrades MRR and recall@1
  monotonically with weight (0.250 → 0.240 MRR from off to 0.5), so
  `THREAD_ARCHIVE_GRAPH_RANK=<weight>` is an experimentation opt-in, not a
  default. Fail-soft without the librarian or without curation.

- Search ranking gains the corpus-native coherence signal, on by default:
  the embedding graph (thread centroids from the shared vector pack → cosine
  kNN → Leiden; zero curation input) came back from thread-librarian into
  `_retrieval/embed_graph.py` along with the shared community spine
  (`_retrieval/community.py`) and its eval (`scripts/graph_eval.py`), and the
  graph deps (networkx/leidenalg/python-igraph) returned to the base install.
  Within a ranked pool, threads whose community carries more of the pool's
  top mass get a bounded boost — measured on the log-mined protocol: recall
  up at every depth past 1 (R@10 0.414→0.433), MRR flat. Coherence applies
  only when the cross-encoder stands down — the two are alternative head
  orderers, and an end-to-end A/B showed stacking coherence under the rerank
  loses what each wins alone (it reshuffles which candidates reach the
  rerank window); with the full stack active the A/B reads neutral, the lift
  lives in the no-rerank regime. The search path never builds the graph
  inline: the warm pass builds it, staleness refreshes in a background
  single-flight thread, and until a build lands the boost no-ops.
  `THREAD_ARCHIVE_COHERENCE=off` disables; a float retunes gamma.
- Documentation accuracy pass: the public API is stated as three things
  everywhere (retrieval MCP tools, truth format, provider plugin API);
  thread-librarian links point at its actual repository; "no server" claims
  now say "no hosted backend" — the MCP server, viewer, and daemons are local
  processes.
- The web viewer has a Playwright browser gate over its production bundle:
  every route must mount without page or console errors, and browser-level
  interaction tests cover search-to-message deep links, thinking controls, and
  model-stat navigation. Network fixtures stay synthetic and cannot read the
  operator's archive.
- Privacy-bearing configuration fails closed: an unreadable, corrupt, or
  malformed existing `config.json` disables all source ingestion instead of
  restoring default-on sources. A bare MCP process is fully read-only and
  catch-up ingestion requires `THREAD_ARCHIVE_MCP_INGEST=1`; setup-generated
  stdio entries carry the opt-in while the shared MCP LaunchAgent pins it off.
  The viewer blocks remote Markdown images and sends CSP, referrer, MIME-sniff,
  framing, and browser-permission response headers.
- The backup mirror's delete-sync now recognizes *renamed* twins alongside
  re-homed ones: a stale legacy-integer-named thread file at the destination
  whose id the ULID migration's durable `ulid-mapping.json` maps to a ULID
  present at both the source and the destination is provably superseded and
  deleted regardless of the deletion cap (`renamed_twins_deleted`). Without
  this, a pre-migration backup kept both generations forever — the cap
  (correctly) refused ~17k deletions, a rebuild loaded every thread twice, and
  the restore drill aborted on FK violations.
- CI now treats resource leaks as test failures. SQLite snapshot connections,
  filesystem iterators, subprocess pipes, HTTP responses, and test database
  probes are closed deterministically instead of relying on garbage collection.
- Coverage floors cover every production package, and the gate fails when a
  future package has no floor. The previously unfloored update, repair, setup,
  provider, and launchd surfaces now have explicit regression thresholds.
- The committed web viewer bundle is rebuilt in a temporary directory and
  compared byte-for-byte in local and public CI. Frontend coverage gates lines,
  statements, branches, and functions, and the real application shell and
  landing route are exercised together.
- Mypy checks untyped function bodies, and generated nested JSON values are
  tested through unknown-field preservation, duplicate import, and truth
  rebuild.
- `backfill_subagent_type.main(argv=None)` parses its arguments with argparse
  and takes them as a parameter, matching every other script in `_scripts/`
  instead of reading `sys.argv` directly.
- `_ops.source_mirror.mirror_sources` takes `watchers=`, the seam
  `check_coverage` and `Watcher` already had: a caller that has resolved its
  provider set sweeps it directly instead of re-resolving the machine's.
- The drift quarantine's per-generation file cap is read per call from
  `THREAD_ARCHIVE_DRIFT_MAX_FILES` (`drift_snapshot.max_files()`, 2000 by
  default), so a store whose active window is larger than the default can be
  covered whole.
- Fixed: the web viewer's status prewarm had no failure guard, unlike the stats
  and curation prewarms beside it — an archive that couldn't be opened at server
  start spilled an unhandled traceback out of the warm thread into the cohosting
  watcher's log instead of leaving the cost to the first real request.
- The `daemon` and `watch` verbs are tested against the operating system rather
  than around it: the LaunchAgent lifecycle runs the real `_launchd` bodies
  against a `launchctl` stand-in that is the only executable on `$PATH` (asserting
  the plist that would be installed and the argv launchctl received), and the
  watch loop runs for real until a real `SIGINT`, cohosted viewer and all.
- The MCP server's command line became a plan it then executes:
  `plan_serve(argv)` reads the transport, the bind, the loopback guard and the
  warm decision out of `argv`, and `main(argv=None)` applies it. The cohosted
  lazy ingest moved onto its gate — `IngestThrottle.claim` / `release` /
  `run_pass` / `maybe_catch_up`, with the kill-switch read through
  `ingest_enabled()` — so the throttle state is the object's rather than the
  module's, and a second gate is a second instance. The stdio and
  streamable-HTTP deployments are now tested as invocations: a real child
  process, spoken to over the real transport.
- `_api.embed` passes an `embedder=` through to the indexer, so a caller
  holding a loaded model — or indexing into a second embedding space — reaches
  it through the coordination layer instead of around it.
- Scheduled release checks are non-mutating by default. The watcher runs
  `archive self-update --check`, records an available tag in status, and leaves
  checkout/reinstall/restart to an explicit `archive self-update`; operators
  can deliberately restore unattended apply with `update.auto_apply: true`.
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
