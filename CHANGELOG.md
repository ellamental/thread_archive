# Changelog

## Unreleased

- Retrieval `fusion_weight` raised 50 → 100. The normalized cross-backend `_rrf` agreement term in
  the weighted ranker was tuned on the discredited title-proxy eval and left the semantic arm
  underweighted against term density: a vocab-mismatch answer the vector arm surfaces (density ~0,
  high `_rrf`) sank under any lexically dense confound (`density*100` dwarfing `rrf*50`). Doubling the
  term lets semantic agreement compete. Measured in production shape (rerank=auto) over the
  snapshot-bound gold files (snapshot `9519fc4518e13ee7`): aggregate success@10 0.909 → 0.945, true
  recall@10 0.708 → 0.746, nDCG@10 0.571 → 0.584, MRR 0.608 → 0.619, success@1 flat, no latency cost.
  Tuned on the query-mined `judged-cases`, confirmed on the held-out topic files (largest held-out
  lift `topic-cases-needle` S@10 0.900 → 1.000, R@10 +0.083; neutral on frustration/suicide; one
  noise-level dip on context-compaction R@10 −0.014). The gains land in top-10 reachability, not
  success@1 — the rank-1 lexical confounds hold, but more real answers reach the window agents scan.

- The `evals/README.md` baseline runbook now leads with `scripts/retrieval_gold_gate.py` as the
  one-command read of the current gold-file baseline: it discovers every gold file, scores each over
  its bound snapshot with the production ranker at the canonical `limit=20`, and prints per-file
  MRR / success@10 / recall@10 / nDCG@10 (the CI gate's measured numbers print on every run, floored
  and ungated files alike). The per-file `retrieval_eval.py --cases` instrument stays the path for the
  fuller metric set and for scoring a challenger on both sides of a change.

- Retrieval evaluation now separates first-hit success@k from true recall@k (the fraction of every case's
  grade-2 gold set recovered) instead of calling success "recall." Reports, the operator CLI, the search lab,
  and graph eval expose both; the snapshot gold gate now protects MRR, success@10, true recall@10, and
  nDCG@10, so losing relevant siblings or degrading the full graded ordering can fail CI even when one answer remains.

- The eval bench sheds the instruments the snapshot-bound gold files supersede. `evals/retrieval_judge.py`
  (pointwise LLM grading of production results — the gold miners now produce graded, corpus-grounded labels
  directly) and `evals/search_arena.py` (blind pairwise LLM duels as the defaults-promotion bar — the promotion
  bar is now a gold-file delta scored on both sides of the change, tuned against one file and confirmed against
  a held-out one) are deleted, along with their guard tests. `tests/test_reality_mechanisms.py` is pruned from
  26 tests to 11: the 15 ranking-preference orderings on synthetic flood corpora go (they were minted from a
  brainstormed edge-case list on the theory that making them pass would improve real search; when the code was
  changed to pass them, measured recall didn't move, and each hard ordering assertion constrained future
  ranking changes) — the 11 deterministic mechanism contracts stay (content-type indexing, MCP default-scope
  widening, reindex durability/stability, semantic scope filtering, cross-encoder gate/window/boundary
  plumbing). Ranking *quality* is now measured in exactly one place: the gold case files. First baseline over
  snapshot `9519fc4518e13ee7`: judged-cases (21) MRR 0.441 / S@5 0.619 / S@10 0.857 / nDCG@10 0.510;
  topic-cases-suicide (7) MRR 0.683 / S@5 1.000 / nDCG@10 0.641. `beir_eval.py` stays as the external yardstick.

- Semantic search no longer rebuilds the corpus vector pack on the request thread. The KNN matrix cache is
  keyed on a whole-store validity token, so continuous background embedding invalidated it every few minutes;
  the next query then read the full ~GB blob table, `np.vstack`'d the matrix, and wrote the pack — inline — and,
  unguarded, a burst of concurrent queries all rebuilt the same pack at once, blowing past MCP client timeouts.
  `_load_matrix` now serves the cached matrix immediately (stale is fine — the lexical arm covers the freshest,
  not-yet-repacked vectors), probes staleness at most once per cooldown, and rebuilds only in a single-flight
  background thread. Redaction can't wait out the cooldown, so it drops the matrix cache outright
  (`reset_matrix_cache`) — dead rows are never served, and the content is scrubbed at the source regardless. The
  corpus-graph refresh (`embed_graph.get`) gets the same cooldown so ingest can't make every search re-probe;
  its authoritative `build()` reads the live matrix directly. The per-query non-emptiness check in the semantic
  arm is an O(1) existence probe instead of a full `count(*)` scan.

- The vector pack is now a **base + delta** so the (now background) rebuild is cheap too. Previously any token
  move rebuilt the whole ~GB base pack — the full blob scan, `np.vstack`, and 814MB write — so continuous
  embedding rewrote it every few minutes. Now a large base pack (mmap, shared across processes) is reused as
  long as it's a clean prefix of the store, and the vectors written since ride along as a small in-RAM delta
  read fresh each build; a `_SplitMatrix` presents the two halves as one matrix to the KNN and the corpus graph.
  A single new vector costs a delta read, not a base rebuild — the full base pack is repacked only when the
  delta grows past `_DELTA_MAX_ROWS` (folding it in) or a delete below the base watermark makes the prefix dirty.
  The token read, base scan, and delta read share one DB snapshot, so a concurrent insert can never land a row
  in both halves or neither. `index_vectors` drops the pack metas only on an actual in-place upsert (an existing
  key re-written, invisible to the clean-prefix check), not on pure inserts, so ingest keeps the base reusable.
  A new parallel stress test asserts that N concurrent `thread_search` calls all serve from the warmed pack —
  none rebuilds on its request thread — and finish well within a bound.

- The CI `retrieval-gate` row no longer runs a from-log metric sweep: it now runs `retrieval_eval.py
  --probes-only --require-semantic --require-rerank` — model-arm liveness checks only. Click-label MRR is
  incumbent-censored (the gold is what the live ranker surfaced and the agent picked), so a per-commit number
  wearing the shape of a quality score invited misreading it as one; quality measurement moves to the
  snapshot-bound gold case files, scored deliberately (`evals/README.md` → "Taking a baseline"). With the
  cadence gone, the nightly's retrieval-trend ledger watcher (staleness + sliding-median alerts) is removed;
  the ledger remains, fed by explicit `--trend-out` runs. `--from-log` stays available as a hand-run collapse
  alarm and as the sampling frame of real query shapes for the gold miner.

- New CI `retrieval-gold-gate` row puts the grounded baseline on the per-commit path — the piece the probes-only
  `retrieval-gate` row deliberately left out. `scripts/retrieval_gold_gate.py` scores every snapshot-bound gold
  case file over its frozen snapshot (`~/.thread/archive-snap`) and fails the row on a drop below a calibrated
  floor. It is a **one-way floor, not a displayed score**: the click-label protocols stay off the per-commit path
  because they are incumbent-censored, but the gold files — grounded and graded — can ride CI as a regression
  ratchet, answering only "did search break below the baseline," never "is search good" (that stays a deliberate
  gold-delta measurement). A stale or absent fixture (snapshot reclaimed, or a gold mid-re-mine) skips that file
  rather than failing, so a maintenance window can't wedge the commit gate red; a freshly minted file rides
  ungated until it gets a floor. Initial floors, a few points under the first baseline over snapshot
  `9519fc4518e13ee7`: judged-cases MRR/S@10/R@10/nDCG@10 floors 0.40/0.80/0.70/0.46 (measured
  0.441/0.857/0.762/0.511); topic-cases-suicide 0.58/0.85/0.78/0.58 (measured 0.683/1.000/0.836/0.642);
  topic-cases-frustration 0.50/0.70/0.50/0.45 (measured 0.600/0.857/0.562/0.511).

- New `thread_archive snapshot <dest>` verb freezes the corpus into a self-contained, immutable archive home:
  it copies the JSONL truth (drain-consistent, under the truth-write lock) and materializes `index.db` beside it,
  restoring the embeddings from the copied vector sidecar without a re-embed. The result is an ordinary
  `THREAD_ARCHIVE_HOME` that any tool — the shipped `eval`, the dev bench under `evals/` — resolves via the
  environment. Because the frozen corpus can't grow underneath a measurement, search over a snapshot is
  deterministic: a regression gate or experiment run scored against one moves only when the code moves, and a
  mined gold can't be outranked by a thread that landed after mining (the isolation the `until` date bound was
  standing in for). `api.snapshot()` exposes the same op; `--vectors` embeds any gap the sidecar lacks, `--force`
  overwrites a non-empty destination, `--no-verify` skips the truth==index check. Each snapshot carries a
  `snapshot_id` — a content fingerprint of its corpus (`_ops.snapshot.corpus_fingerprint`) that reproduces on a
  plain re-snapshot but changes whenever the corpus does.
- Agent-mined retrieval golds are now bound to a corpus snapshot instead of a per-case `until` date bound.
  `retrieval_mine_gold.py` requires `THREAD_ARCHIVE_HOME` to be a snapshot, searches/reads that frozen corpus
  (no more server-side date bound), and stamps each case with the snapshot's `snapshot_id`. `retrieval_eval.py
  --cases` runs over that same snapshot and refuses any case whose `snapshot_id` doesn't match the home — a
  corpus that has moved on invalidates its golds loudly rather than scoring them against drifted data. The
  `evaluate()` `strict`/`until` plumbing and the `--strict` flag are gone (the snapshot subsumes them). Existing
  mined case files (which carry `until`, not `snapshot_id`) are invalid under the new binding and must be
  re-mined against a snapshot.
- Topic-based gold mining is now a committed script (`evals/topic_mine_gold.py`) instead of an ad-hoc agent
  process. It mints golds from a curated topic dense with confounds in two agent stages: a survey `claude` agent
  searches the topic, decides how many *angles* it warrants (its own call — no target count), and authors one
  query per angle with the intent, the confound subjects, and the candidate threads its searches found; then one
  labeler agent per angle takes those candidates as a starting pool, verifies and expands them with its own
  searches to find everything relevant, and grades a comprehensive pool (2=intended, 1=partial, 0=confound). The
  labeler builds on the survey's findings rather than rediscovering blind — the goal is the most complete gold
  set, and the labeler isn't the search system under test, so nothing leaks. Snapshot-bound like the query miner
  (requires a snapshot home, stamps each case with `snapshot_id`), resolves a topic by id or unique name, and
  writes the eval's `--cases` format (`topic-cases-<slug>.jsonl`) plus a facet-map/intent detail sidecar. The
  reusable headless-agent runner is factored into `retrieval_mine_gold.run_claude`, shared by both miners.
- Retrieval closes the last nine reality-mechanism goldens (formerly expected failures). The cross-thread
  duplicate fold is now a *near*-duplicate fold — `rank._norm_content` folds runs of digits to one placeholder
  before comparing, so a flood of threads differing only by a counter or run index (routine ops, re-asked
  questions, pending-todo restatements, injected boilerplate carrying a `task N`, a swarm of agents on one
  templated prompt) collapses to a single representative instead of filling the ranked window and burying the one
  terse or old authoritative thread the query wants. And the MCP `thread_search` default scope (user/title/summary),
  when it comes up dry, widens once to the whole transcript rather than to assistant text alone — so an answer that
  lives only in a tool result, a tool's error, or the assistant's reasoning is reachable; the widen keeps its result
  when it surfaces a strong match anywhere, so a low-weight tool/thinking hit counts even below a weak user hit.
- Retrieval closes seven ranking failure shapes (the reality-mechanism goldens, formerly expected failures):
  a duplicate-flood rescan folds byte-identical bursts to one representative per `(thread, content)` from a bounded
  rank window, so the distinct answer a fleet-of-copies buried still reaches the pool (and with it, a literal
  `frobnicate_widget` no longer loses to split-token prose); ranking term-matching is word-aware (`auth` stops
  scoring inside `author`, `cache` still credits `caches`); the reranker window centres on the densest term cluster
  and a long doc also offers its head and tail (MaxP), so an answer far from an incidental term is scored; its head
  reaches at least `limit` deep so a strong-but-sparse hit at the pool boundary is reachable; a verbatim query echo
  no longer stands the cross-encoder down, though its verdict is kept only when it rescues a lexically-weak
  (vocab-mismatch) hit rather than reshuffling confident ones; and the MCP default scope widens to assistant text
  whenever the top hit is below the strong-match bar, not only when no term landed.
- Python floor drops from 3.14 to 3.12: nothing in the code needs 3.14, so the install now runs on the Python
  most machines already ship. Classifiers, the CI matrix (3.12 floor + 3.14), the install-test container, and the
  four install docs follow.
- Install docs close three friction gaps: the macOS path checks for the Xcode Command Line Tools the base
  C-extensions (igraph/leidenalg/cryptography) need on a source build — the Ubuntu and Docker paths already install
  build-essential; and the README pitch plus both agent install docs now state that the clone's location is
  load-bearing — `.mcp.json`, the service units, and self-update bake its absolute path, so relocating it means
  re-running the wiring, not a plain `mv`.
- Docs repositioned around preservation as the product: search framed as the access layer, eval metrics and
  methodology move to docs/search-quality.md, the related-projects survey to docs/related.md; the README stops
  claiming macOS-only (Linux/systemd is real and CI-tested), counts 8 shipped harnesses (cloth is an operator
  plugin), links the Ubuntu install path, and documents the embed / mirror / eval verbs.

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
