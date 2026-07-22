# Changelog

## Unreleased

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
