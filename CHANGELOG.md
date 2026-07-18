# Changelog

## Unreleased

- **Curation moves out of the core into the archive-librarian plugin** — the
  interactive curation surface (the `/librarian` skill, a new `/gardener`
  skill built from the drain prompt, the librarian-gate enforcement hook, the
  write-MCP wiring) leaves `.claude/` and becomes a
  Claude Code plugin at `plugins/librarian/`; the repo is its own plugin
  marketplace (`.claude-plugin/marketplace.json`), so
  `claude plugin marketplace add <clone> && claude plugin install
  archive-librarian@thread-archive` installs it anywhere, not just in the
  clone. The core install correspondingly shrinks to preservation + retrieval:
  `.mcp.json.example` and the wizard's MCP wiring carry only the read server
  (`archive-mcp`) — a wizard-wired client no longer gets curation power by
  default — and the wizard drops its scheduled-curation step and
  `--skip-curation` flag. The knowledge layer, the librarian MCP server
  (`archive-librarian-mcp`, which the plugin launches), and the headless
  drains (`archive curate`, `archive daemon install --librarian/--gardener`,
  their packaged prompts in `_curation/`) all stay in the package — existing
  scheduled drains and curated graphs are untouched; the gate hook also
  recognizes the plugin-namespaced `/archive-librarian:librarian` invocation.

- **Viewer folds threads that repeat one line** — searching a common opener ("hey
  grok") spent the whole result page on N threads showing the same text: the MCP
  surface folded cross-thread duplicates, the viewer passed `group='none'` and
  listed every hit. Search grows a third grouping mode, `group='dup'`
  (`rank.fold_duplicate_threads`), that folds only the cross-thread twins and
  keeps each surviving thread's own hits — the reader's shape, where
  `group='thread'` collapses a thread to one row for an agent spending result
  slots. The viewer uses it and renders the fold as a collapsed
  "same text in N other threads" expander, `/api/search` resolving the folded ids
  to titled links so a hidden thread stays reachable.

- **Viewer inherits the agent contract's orientation signals** — the web viewer's
  search now carries the three things the MCP surface had and it didn't: an
  **empty query browses** (one row per thread by last activity — source, event
  count, tail-anchored open — honoring the same source/date filters; Enter on an
  empty search box lands there), the results line shows the **match-quality
  verdict** (strong/partial/weak/semantic, with the caution note and per-hit K/N
  term badges), and results name the **subjects** they cluster under as chips
  linking into the topic pages. `/api/search` grows `browse`, `quality`, and
  `subjects` fields plus per-hit `term_hits`, reusing the retrieval layer's
  existing browse/quality/subjects machinery — no logic duplicated in the web
  layer.

- **Install story: the clone is the install** — the README drops the `pip install git+…`
  front door (a leftover from the retired PyPI run; 0.0.3 already moved distribution to
  clone-install) and now leads with the agent path: clone, open Claude Code, "install
  this — follow claude-install.md". The old "clone path — for curation" framing was
  stale twice over: the librarian/gardener drain prompts ship inside the package and the
  wizard schedules curation on any install, so what the clone uniquely carries is the
  interactive `/librarian` skill + gate hook and the project-scoped `.mcp.json`.
  `thread_archive` is reframed as the setup wizard / always-on upgrade rather than a
  rival front door; the wizard's semantic-search hint and releasing.md's distribution
  line now speak clone-install, and claude-install.md's Done step points at
  `thread_archive setup` for watcher/backup/scheduled-curation.

- **Importer dropped-field sweep** — a full audit (prompted by the cloth `cost` bug) found
  every importer silently losing source data at one of two seams: the parser never read a
  field, or the builder dropped what the parser extracted. All fixed. The shared mechanism
  is a new **annotations channel**: parsers put message-level extras in
  `provider_data["annotations"]` and block-level extras in `block["annotations"]`; the
  builder copies them onto the event payloads under `annotations`, deliberately outside the
  dedup content keys — so identities stay stable, re-imports stay idempotent, and stored
  events are enrichable via amendment. Per source: **opencode** cost/usage/stop_reason
  (previously all zeroed), API + tool error detail, synthetic-text flags, project_id;
  **codex** per-turn token usage (was 0 on every turn), pasted user images (were dropped
  outright), git provenance in source_metadata, effort/personality; **claude-code/cloth**
  effort, MCP/skill attribution, structured `toolUseResult`, tool-denial kind, mcpMeta,
  permission mode, origin, todos, request/message ids, git branch; **cursor** real model
  names (was the literal `"cursor"`), token usage, compaction summaries as
  `context_summary` events, attached-code context; **antigravity** thinking blocks
  (thinking-only steps were dropped whole), error text on failed tools, truncation
  markers; **claude-science** per-message tokens and frame-level cost/token totals
  (~$20/15M tokens were invisible), artifact/cell-image refs, rolling summaries;
  **cowork** session cost/usage stats into source_metadata; **grok** model_fingerprint,
  mid-turn-abort markers, session reasoning/kind config; **exports** claude.ai tool
  pairing ids (100% of tool events were unpaired), citations, thinking summaries,
  structured tool content, safety-flag blocks, `parent_message_uuid` branch metadata,
  ChatGPT citations/content_references/canvas/assets, tether browsing text (was empty),
  code-block language, and conversation-level metadata folded into thread
  source_metadata. Structural guard: a **field-level drift ledger**
  (`known_line_fields`/`known_message_fields` on ProviderConfig) makes a NEW field on a
  known line type warn through the validation ledger instead of vanishing — the exact
  blind spot that hid `cost`. Backfill scripts (`backfill_dropped_fields`,
  `backfill_export_annotations`) enrich already-imported threads through the amendment
  seam, with struct-anchor salvage for hollow errors / cursor model names and a guarded
  dedup-key rewrite for claude.ai tool pairing.

- **Images and attachments are viewable** — binary payload content (pasted screenshots,
  tool-result captures, base64 documents) now extracts at import into a content-addressed
  blob store (`truth/blobs/<hh>/<sha256><ext>`; exactly invertible, so dedup keys and the
  verify hash gate hold — a lost blob file is a red check, not silence). MCP reads render
  `[image image/png 48 KB — /path]` markers an agent can Read; the web viewer shows images
  inline via `/api/blob/<hash>`; historical inline base64 materializes lazily on read (no
  migration); redaction shreds blob files (unless shared) and bundles the content for
  unredact; tool-result lists render their text instead of a `str(list)` repr.

- **`thread_read(mode='ends')`** — a head+tail view: the first and last `context_turns`
  turns (default 1 each end) chat-style in one read — "what was this session and how did
  it end" without paying for the middle. A gap marker names the `mode='chat'` offset that
  continues past the head; the `max_chars` budget splits across the ends and trimming
  keeps the outermost turns (the opening ask, the closing answer).

- **`thread_read(mode='last')`** — a token-minimal view that returns only the thread's
  closing assistant text (the final answer / wrap-up), with its event anchor, turn
  position, and a one-call hint to open the surrounding exchange. The cheapest "how did
  this session end" read; previously that cost a whole last-turn `chat` read. Budgeted
  by `max_chars` like everything else; a thread ending on an unanswered user message
  returns the latest assistant text there is.

- An **empty `thread_search` query is now a browse** — the agent surface's missing list view.
  One row per thread, ordered by last activity (newest event's `occurred_at`, falling back to
  `updated_at`), under the existing structural filters: `since`/`until`, `source`, `limit`,
  `sort='oldest'`, `topic_id` scope. Rows carry the thread id, source, type, event count, and
  the newest event id as a ready `around_event` anchor; `output='linkable'` stays JSON. A new
  `types` filter (comma-separated `thread_type` values) picks the population — a browse
  without it hides `topic`/`system` threads (the web recent-list default); with a keyword
  query, `types` scopes the lexical arm the same way (semantic arm sits out). Previously an
  empty query returned "No results" — "what happened yesterday" / "list recent cursor
  sessions" required guessing keywords.

- **`thread_read('topics')` renders the curated topic tree** — the knowledge graph's table of
  contents on the public read surface: an indented forest over part-of/contains links,
  biggest subtree first, budgeted by `max_chars` with a clean truncation note; the header
  counts unparented topics and points at `thread_search('', types='topic')` to list them.
  The tree builder moved to `_knowledge.topic_tree()`; the web viewer's `/api/topics/tree`
  delegates to it.

- Ranked search results are **grouped one row per thread** (`rank.group_by_thread`): a thread's
  best hit represents it, further hits fold into a `+N more in thread` annotation, and duplicate
  content from other threads (forked sessions, fleet-spawned copies of one prompt) folds into a
  `= same content in thread(s) …` annotation — the fold annotates the surviving row instead of
  spending result slots repeating it. `group='none'` (on `thread_search` / `api.search`) restores
  every-hit-a-row; a `thread_id` scope, the structural shapes (browse/startswith/oldest), and
  `count`/`linkable` output are never grouped; the web viewer's `/api/search` passes
  `group='none'` (its UI lists every hit). Independently, every row-shaped output now collapses
  hits sharing one `(thread_id, event_id)` anchor — a thread-meta title/summary doc and the first
  event it anchors to could both match and render as two rows that open identically. The
  cross-encoder re-rank now scores only the top `RERANK_POOL` ranked candidates (its documented
  intent) rather than up to `limit` when `limit` exceeds the pool.

- Agent-run threads (`thread_type='system'` — Task-tool subagents, machinery runs) are now
  **excluded from search and browse by default**: swarms echo their spawning prompts verbatim,
  and those copies were outranking the conversations that asked. A new `agents` control on
  `thread_search` (and `api.search` / `browse_threads`) picks the inclusion: `'exclude'`
  (default) / `'include'` / `'only'` ("what did my subagents do"). Applied in both retrieval
  arms (lexical WHERE + vector hydration, with a KNN pre-mask for `'only'`). Deliberate scopes
  stand it down, mirroring the blacklist: an explicit `thread_id`/`topic_id` bypasses it, and
  an explicit `types` list wins over it entirely.

- The cross-encoder re-rank gains a **result-side gate** (`rank.head_is_strong`): after ranking,
  a top hit that literally contains ⌈2/3·N⌉ of the query terms (the header's own `strong`
  threshold, now shared via `rank.strong_match_floor`) skips the re-rank — the stage only pays
  its seconds on the vocab-mismatch queries it was built for. Motivation: profiling showed the
  reranker was ~4.9s of a ~5.7s warm search, and a 150-title eval measured forced re-rank
  *degrading* lexically-anchored queries (MRR 0.546→0.482) while the paraphrase eval that
  justified the stage stays covered (weak/partial heads still re-rank). `rerank=True` still
  forces the stage past both gates; the warm pass uses that so the model still preloads.
  `term_hit_count` moved from `format` to `rank` (format re-exports it).

- Concurrent `save_vectors_sidecar` calls (a backup racing the watcher's cadence, two operator
  sessions) no longer collide: each saver builds under a pid-unique temp name (the fixed
  `vectors.sqlite.tmp` let one saver ATTACH another's half-built database — a CREATE TABLE
  error at best, publishing a half-built sidecar at worst), stale dead builds are swept by
  age, a failed save removes its own build file, and a DETACH failure invalidates the pooled
  connection instead of returning it with a stray `side` schema attached.

- **Event amendment** (`thread_archive._ops.amend`, API `amend`/`amendments`): the sanctioned
  append-only edit mechanism — a superseding truth line (same event id + dedup_key, merged payload)
  written through the ordinary staged-drain seam, with the prior line preserved as history and an
  audit record (fields, before-values, reason) in `truth/amendments.jsonl`. Restricted to
  non-content fields (content-hash material is redaction's jurisdiction), so re-import idempotency
  and every verify/rebuild hash gate hold. Readers already reconcile: loads are last-wins by id.
- `backfill_usage_cost` script: re-parses each source transcript and amends the `cost` +
  cache/extra-usage fields the old importer dropped onto stored `api_request_completed` events
  (dedup_key match, content-anchor fallback; missing-only merge; dry-run by default, idempotent).

- Each model on the stats page links to a **per-model drill-down** (`/stats/model/:model`, backed by
  `/api/stats/model/<name>` — the name is a percent-encoded path tail, so router ids like
  `deepseek/deepseek-v4-pro` work): overview tiles (sessions, tokens, requests, compactions, cost where
  recorded), a per-session token distribution (min/median/avg/max), a monthly series (sessions, tokens,
  compactions — sessions bucket into the month they started via `threads.inserted_at`, since surveying
  per-request timestamps from the event log is seconds-slow; compaction events carry their own month),
  and the heaviest sessions linking into the reader. Compactions are `context_summary` events counted
  across the sessions the model took part in — the event doesn't record which model's context overflowed.
- The web viewer gains a **stats** page: token and cost analytics over the whole archive — overview
  totals + activity span, a by-provider table (conversations, tokens, and average session cost for the
  pay-per-token sources that record it), and a by-model breakdown listing every model used. Cost is read
  straight from the `api_request_completed` payloads that carry it (subscription tools log tokens but no
  dollar figure, shown honestly as "—"). Backed by an incrementally-maintained per-(thread, model) rollup
  (`thread_metrics` + a `metrics_cursor` watermark) that folds only new events, so the survey stays fast
  on a multi-GB index; the rollup is a derived projection that rebuilds itself after a reindex.
- Archive-operational (curation drain) threads are created with `exclude_from_search` set, and existing
  ones were backfilled — librarian/gardener transcripts quote search hits wholesale, so they matched
  nearly any query about their own subjects. Subagent `system` threads stay searchable.
- Web viewer search snippets show the matched line plus one line of context on each side and unwrap the
  Grok `<user_query>` wrapper to the real prompt — the result list reads like the thread it opens instead
  of leaking `<user_query>`/`<user_info>` tags. The status bar polls instead of fetching once, so a
  transient blip (a watcher restart cycling the cohosted server) no longer latches a permanent "archive
  unavailable" banner.
- The topic graph is followable from search for read-only consumers: the `subjects:` header carries each
  subject's `[topic <id>]`, a hint line teaches the moves, and the docstrings advertise that `thread_read`
  on a topic id renders its curated page (description, links, cited quotes).
- Stored summaries yield to verbatim evidence in ranking (content-type weight 1.2 → 0.6): a librarian
  digest stays findable but no longer crowds the record it summarizes out of the top ranks.
- Curation drains spawn from `<home>/curation`, and the claude-code importer files any session run from
  inside the archive home as a hidden `system` thread (`archive_operational`) — a drain's own transcript
  no longer re-enters the librarian queue for future drains to summarize.
- Backup drops all disk-durability posturing: the `same_device` flag, the Time Machine probe
  (`external_disk_coverage`), the same-filesystem warnings, and every "different disk/machine" /
  off-machine suggestion in docs and wizard copy. Disk durability is the user's concern, like any
  other data; the backup's scope is recovering from bad writes. "Durability kit" is now the backup kit.

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
