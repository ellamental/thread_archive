# Changelog

## Unreleased

- **The log-mined eval survives the ULID migration, and the proof story grew
  teeth.** The `--from-log` miner resolved read refs by integer comparison
  against `threads.id`; ULID primary keys made that intersection empty, so the
  protocol mined zero cases and the CI retrieval-gate went red (the collapse
  alarm firing for a real collapse — of the eval, not the ranker). The miner
  now canonicalizes every trail ref (legacy integer id, ULID, provider session
  id) through `resolve_thread_ref`, the same resolution `thread_read` itself
  applies; 563 cases mine again and the gate is green at MRR 0.228 / R@10
  0.387 (floors 0.14 / 0.20). Around that fix, three new instruments close
  the click labels' known gaps: `--trend-out` appends any run's report as one
  JSONL row and the gate row now writes `~/.thread/archive/retrieval-trend.jsonl`
  every sweep (quality as a time series, not a launch-day number);
  `--behavior` reports zero-label usage signals — per search: clicked,
  reformulated, or abandoned — from the whole trail; `--mined-after` is the
  time-based holdout (only cases mined after a date, for judging a ranking
  change on post-change usage). New `scripts/retrieval_judge.py` sends a
  sample of mined queries through the production stack and has a headless
  `claude` grade every top-10 thread — graded precision, click-label
  calibration, and beyond-click credit the click protocol structurally can't
  award. The rerank/fusion docstrings now name the protocol behind their
  tuning numbers (title-proxy) instead of citing them as if real-query.
  `scripts/topic_eval.py` gives the knowledge graph a usage meter aimed at
  its actual delivery path: the relevant-subjects lens annotates every
  search result set with openable topics, so the headline metric is
  **subject uptake** — how often a topic read follows a `thread_search`,
  split by working sessions vs. curation machinery, with cold topic reads
  (tree navigation) counted alongside. First measurement: 0.2% of all 3,408
  trail searches ever led to a topic read — but the lens shipped 2026-07-15,
  and post-ship the rate is ~9% in working sessions (5/56; small n, real
  signal). Secondary and labeled curation ergonomics: the librarian's dedup
  `topic_search` under the click protocol — 4,547 calls, 16% found a topic
  to act on, 4.6% created one instead, 79% nothing; re-findability of
  acted-on topics MRR 0.244 / R@10 0.343 (title-substring match hits at
  rank 1 or never: R@20 ≈ R@10).

- **The test suite runs four-wide, and one test stopped deleting the database out
  from under a live engine.** The `pytest` row swept 2147 tests in a single
  process; it now runs `-n 4 --dist=loadfile`, taking the row from ~240s to ~50s
  with coverage still gating (a file's tests stay on one worker, and coverage is a
  union over the workers, so the floors gate the same numbers). Parallelism
  surfaced a latent bug it did not cause: `test_verify_hash_gate_and_reindex`
  unlinked `index.db` while the module-global engine still held pooled connections
  to that inode, so the next connection reused could raise `disk I/O error` from a
  `PRAGMA` against a deleted file. It closes the archive first now, the way every
  other test that deletes a live index already did.

- **A new install curates what happens next, not what it found.** Scheduling the
  librarian stamps a **curation horizon** (`curation.librarian.horizon`, set at
  install): conversations from that point on are curated as they happen, and the
  sessions already on disk are history, left alone by default. Without it, an
  install pointed at years of existing transcripts opens with a five-figure queue
  and bills an unattended Opus run against it every hour for weeks — the drain is
  hourly and batch-capped, so a large history is a long, expensive tail nobody
  asked for. History is reached through a **rate**, not a switch:
  `curation.librarian.catchup_per_run` (default 0) sets how many older threads
  ride along in each run, always *behind* that run's live work, and the queue
  flags them `catchup: true` so the drain knows why an old thread is at the
  bottom of a newest-first list. The gate mirrors the split and counts history
  only up to one run's worth, so a drained forward queue still skips its fire
  instead of launching into work it would only nibble. The horizon compares
  against when a conversation *happened* (`occurred_at`), not when it was
  ingested: a first import stamps its whole history as arriving today, so ingest
  time cannot tell a decade of transcripts from this morning's. A session that
  straddles the horizon counts as forward. Setup offers all of this
  (`thread_archive setup`, or `--curation --catchup-per-run N`), as does
  `archive daemon install --librarian` (`--catch-up` opts into the whole
  archive). An existing install without a horizon is unchanged: the queue stays
  undivided. Recurring spend is never opted into by silence — with no terminal
  to answer, curation schedules only when asked for outright.

- **The librarian's queue has a floor, and the drains no longer pay to
  rediscover it.** `review_queue` treated any thread with a row in `events` as
  curatable, but a session can register a thread and write only bookkeeping (an
  empty `file_snapshot`, a `queue_operation`) — no message to cite, no content to
  summarize. Those threads could never leave the queue, and since it is
  newest-first they collected at its head: on this archive, 41 of them, re-read
  by every hourly drain, one of which curated nothing at all. Eligibility now
  requires an event whose type is in `INDEXABLE_EVENT_TYPES` — the same set that
  decides whether an event reaches the search index, so the rule is one rule: if
  it can't be found, it can't be curated. `_curation.librarian_backlog` carries
  the identical clause (it reads the type list from the extractor rather than
  restating it), which matters more there than in the queue: the daemon already
  skipped a fire on an empty queue, but was being handed a count that could never
  reach zero. Content-free threads are an ingest condition, not backlog, so they
  are now *counted* rather than silently excluded — see the curation page.

- **A curation page in the viewer (`/curation`), and `/api/curation` behind it.**
  What the two unattended drains have done, in one place: each drain's remaining
  backlog (read through the same gate the daemon fires on, so page and daemon
  cannot disagree), cadence, model and liveness; per-day citations, links and new
  topics; topic-graph health; summary/citation coverage; the content-free thread
  count; and the drains' own cost — the runs archive themselves like any other
  session, so the archive reports what curating it cost. Cost is requests and
  output tokens, never dollars (the drains run on a subscription login that
  records no per-token price) and never input tokens (the recorded figure excludes
  cached context). There is no per-day summary count: a stored summary has no
  set-time of its own, so summaries appear as coverage instead of an invented
  series. New `_curation.stats`; the viewer's status cache generalized to serve
  both surveys, with a cold-fill guard so a page load racing the startup prewarm
  waits for that pass instead of starting a second one against the same database.

- **Thread ids are ULIDs (truth format v2).** `Thread.id` moved from a
  SQLite-autoincrement integer to a 26-char Crockford-base32 ULID minted at
  creation (timestamp = thread start, so id order is chronological and ids are
  globally unique — a future second archive can merge without collision or
  rewrite). The old integer ids live on as `Thread.legacy_id`, a permanent
  alias: `resolve_thread_ref` now resolves three ref shapes by form alone —
  all-digits → legacy alias, 26-char base32 → primary key, anything else →
  provider session id — so every integer id ever pasted into a conversation
  keeps resolving. Truth sharding now buckets by sha256 of the id string;
  `TRUTH_FORMAT_VERSION` bumped to 2; the threads table dropped its
  autoincrement high-water machinery (events/kg_events keep theirs). One-shot
  migration: `thread_archive._scripts.migrate_thread_ulids` (build-new →
  swap → reindex; the pre-migration truth is kept in `pre-ulid-backup/`, the
  id mapping in `ulid-mapping.json`). All consumer surfaces (MCP retrieval +
  librarian tools, web viewer + frontend, CLI, importers) accept string ids
  and keep accepting legacy integers as refs.

- **The product is Claude Code-first, and a drifted import is now locally
  repairable end to end (`archive fix-import`).** The positioning change
  (README): Claude Code is the supported, first-class source; every other
  harness is best-effort and community-maintainable. The machinery change is a
  full self-repair loop for provider format drift, built on the seams that
  already existed. Detection: the skip/validation ledger summaries gained
  per-source breakdowns, and the coverage check composes them (with its
  went-dark / stale-ingest FAILs) into per-source **degradation verdicts**
  (`{reason, since}`) persisted in health.json's `coverage_last` — sustained
  ledger volume (≥3 recent records) degrades; a single benign record still
  only warns. Preservation: a degraded source's recently-active raw store
  files are snapshotted into `dumps/drift/<source>/<stamp>/` (new
  `_watcher/drift_snapshot.py`, riding a new `SourceWatcher.store_paths()`
  hook; incremental generations, bounded and loud about truncation, never
  auto-deleted, SQLite via the backup API, source_ids recorded for replay).
  Notification: the MCP server prepends one `note:` line per degraded source
  to `thread_search` results naming the remedy (unconditional on the result
  set — a degraded source's freshest content is exactly what search can't
  return; verdicts older than 14d are ignored so a dead nightly can't nag).
  Repair: `archive fix-import <provider>` scaffolds an override patch under
  `<home>/plugins/<provider>/` — patch module built on the new public
  `provider.builtin(name)` helper, pre-wired test suite (fixtures import,
  no validation findings, and a watermark-reset re-import dedup guard — the
  one way a fix corrupts rather than degrades), collected samples (ledgered
  files first), drift evidence digest, per-provider quirk docs (claude-code's
  is substantive; others get the generic doc) — then spawns a headless
  `claude` (curate's spawn shape: repo-hosted prompt, `--print`,
  strict-empty MCP config, group-kill timeout; foreground, since the user
  invoked it) whose only job is parse logic. `--activate` is the
  deterministic gate: module loads and resolves to an override of the right
  name, scaffold suite green in a fresh subprocess, only then `enabled: true`
  in config.json, registry reset, and the ledger-driven re-import (reset
  ledgered watermarks → one poll; drift-quarantine copies whose originals
  were pruned replay through the importer). Lifecycle: patches record
  `built_against` and are temporary by default — self-update's new injectable
  `retire` collaborator (after smoke, before restart) disables unpinned
  patches built against an older core, with no cleverness about whether the
  release fixed that provider (if drift persists the notice re-fires and the
  fix re-runs); `--pin` opts out ("I always want mine"); hand-installed
  plugins without a `patch` block are never touched. Every transition lands
  in a new `patch-log.jsonl` audit ledger; `archive providers` shows
  `patched` / `patched (pinned)` / `patch retired`; `archive coverage` prints
  the degraded verdicts and any snapshots taken. docs/providers.md documents
  override patches and `builtin()`; the README carries the honest promise:
  the supported provider's worst case is preserved but partially modeled
  until fixed.

- **Retrieval is now measured against real usage, not just the title proxy.**
  The eval harness gains a `--from-log` protocol: every `thread_search` an
  agent has run is itself archived, along with the `thread_read` that followed,
  so the archive's own tool-use trail is a click-labeled query log (561 cases
  mined at introduction, legacy thread-commands calls included — the thread id
  space carried over). Real-query numbers land far below the title proxy's
  (fused MRR 0.245 / recall@10 0.43 vs 0.64 / 0.89): title-as-query overstates
  quality because titles are LLM distillations of the threads they name. The
  librarian-summaries lift the title protocol showed (+0.09 MRR) vanishes on
  real queries (+0.001) — vocabulary correlation between two distillations of
  the same thread, not retrieval value. The README's measured section now
  reports the real-query table. The two CI retrieval rows collapsed into one
  lean gate: `--require-semantic` asserts the embeddings arm is alive directly
  (the failure a metric floor detects worst — fused silently degrading to
  lexical — checked with zero queries), and a small seeded sample of log-mined
  cases serves as a collapse alarm whose floors sit far below measured and are
  never ratcheted (click labels are shaped by what past search surfaced, so a
  modest dip under a reshaped ranker is not evidence of regression). The
  title-as-query row is gone from CI; the protocol remains in the harness as a
  quick local probe. `--cases FILE` accepts curated JSONL sets; the pairing
  and scoring logic grew unit tests.

- **The type gate is green again and the coverage ratchet moved up.** Eight
  mypy errors had accumulated across the importers, the source mirror, the
  retrieval pipeline and the web server; `EventHit` now declares the two
  enrichment keys the web layer stamps on it (`term_hits`, `dup_threads`)
  rather than the checker rejecting writes the viewer depends on. The backfill
  scripts and the newer CLI verbs (self-update, mirror, providers) had landed
  without tests, dropping `_scripts` to 79% and `cli` to 89%; both are now at
  99% and their floors — plus TOTAL — are raised to follow.

- **One unreadable export bundle no longer aborts the whole
  backfill-export-annotations run.** `_iter_conversations` is a generator, so
  the `try` around the call caught nothing — the classify/load work (and its
  `ValueError` on a path that is not an export) ran on first iteration, outside
  the guard. The `bundle_errors` counter was unreachable and a single bad path
  in a multi-bundle run took the rest of the bundles down with it. The
  conversation list is now materialized under the guard.

- **The README opens with the payoff, not the machinery.** A day-one demo block
  — your existing `~/.claude` history imported, then answered mid-conversation,
  with the eval numbers as the receipt — now leads; the durability/"built like
  a database" block moves below the measured table.

- **Single-machine is now a stated boundary, not an assumption.** The README
  gains a "Not supported" section naming what the product deliberately won't
  do: multiple machines and archive merging, non-macOS platforms, multiple
  users, live web-chat capture, and driving a conversation. The merge question
  is the load-bearing one — thread and event ids are locally-minted integers
  baked into the truth layer (filenames, record bodies, the curatorial log,
  and inline citations in summaries), so independently-grown archives share an
  id space with nothing to distinguish them. Supporting a merge would mean
  either partitioning the id space per machine or moving to natural keys; both
  cost the citation ergonomics the librarian depends on, and neither buys
  anything for a single-user, single-Mac product. Moving an archive between
  machines is unaffected and still supported.

- **Installs update themselves from the release tags.** Provider formats drift,
  and a parser fix is worthless on a machine it never reaches: the watcher now
  probes daily and fast-forwards the clone to the newest annotated tag once it
  has soaked 48h — fetch → checkout → `pip install -e` → smoke check (`archive
  status` under the new install) → restart the long-running daemons, rolling
  back to the previous commit if the new install doesn't stand up. Hard refusals:
  a dirty tree, local commits off the release line, and (unattended) a
  `TRUTH_FORMAT_VERSION` bump — that one-way door needs a human
  (`archive self-update --allow-format-bump`). Manual verb `archive self-update`
  (`--check` to report only); outcome on `archive status`'s new `update:` line;
  config `{"update": {"enabled": …, "min_age_hours": …, "remote": …,
  "check_interval_hours": …}}`, default on. Pushing a release tag is now
  *shipping*; docs/releasing.md gains the yank procedure (delete the remote tag
  inside the soak window, then release a fixed higher version).

- **Raw source mirror: the harness stores are preserved verbatim.** `archive
  mirror` (and a new first stage of the nightly) copies every transcript file
  the enabled watchers consume — plus small JSON sidecars, plus SQLite-backup
  snapshots of the live DB stores — gzip-compressed under
  `<home>/source-mirror/<provider>/<original path>.gz`, and never deletes.
  Until now the harness's own store was the only raw layer, so every
  after-the-fact source re-read (a parser taught a new field, a backfill
  repairing an importer bug) raced Claude Code's ~30-day prune; the mirror
  ends that race. Unchanged files are stat-skipped via a per-provider
  manifest; a shrunken transcript rotates a numbered generation instead of
  overwriting the only copy.

- **A new field on a modeled line is now preserved, not just warned about.**
  The field-level drift check's finding used to be literal data loss: the
  value rode into `provider_data` and died at the builder seam. The import
  seam now writes the *residual* — every raw-line key outside the provider's
  field ledger, values included — onto the anchor event as
  `annotations["unmodeled"]` (`parsers.residual`; one computation shared with
  the validator, so warning and preservation cannot disagree). Annotations sit
  outside dedup identity, so nothing forks. A turn with no anchor (a
  tool-result-only line) attaches them to its first emitted event.

- **Version tripwire.** The first sighting of a new harness version string on
  a source line writes one advisory record to the validation-drift ledger
  (state in `<home>/seen-versions.json`, bundled by backup). Format changes
  ride version bumps, so this warns before any field drifts — and names the
  release when one does.

- **Active drift now pushes.** The ledgers were pull-only: drift was a
  coverage *warning*, coverage warnings never fail the night, and the nightly
  only notified on failure — so 671 drift records sat unread for a week. The
  nightly now sends a second, softer notification whenever either capture
  ledger took records in the last 24h; it goes quiet on its own the day after
  the ledger does.

- **Workflow subagent transcripts were never captured.** The Claude Code watcher
  globbed `*/subagents/*.jsonl` — exactly one level — while a workflow run nests
  its agents two further down (`subagents/workflows/<wf-id>/agent-*.jsonl`). Every
  one of those transcripts was invisible to ingest; 499 sat unarchived on this
  machine. The glob is recursive under `subagents/` now, and matches the
  `agent-` prefix rather than `*.jsonl` — a workflow directory also holds a
  `journal.jsonl` run ledger, which is not a transcript and whose name repeats
  once per run, so matching it would land every run in a project on one
  source_id. Capture-coverage could
  not have caught it: `discover()` reports what `iter_files()` yields, so both
  sides of the reconciliation shared the blind spot and it read as a clean 100%.

- **The agents' open-file ceiling is raised to 4096.** launchd hands a process a
  soft limit of 256 descriptors, and the truth log's own append-handle cache
  (`MAX_OPEN_HANDLES`) is sized at 256 — leaving nothing for the database, the
  locks, the vector pack or the logs. A pass touching many threads at once died
  partway through on `[Errno 24] Too many open files`, and because the fingerprint
  seam only advances after a successful import, it retried the same files forever
  without progressing. Latent until an ingest pass got big enough to reach it.

- **Subagent threads record which kind of agent ran.** Claude Code names it in
  `attributionAgent` on every assistant line (`Explore`, `general-purpose`, a
  custom agent name …); the importer read the transcript's `agentId` but not that,
  so a subagent thread knew which run it was and whose child it was, but never
  what it was. Now stamped as `source_metadata.agent_type` at import — once per
  thread, since both fields are constant across a transcript.
  `_scripts/backfill_subagent_type.py` fills in threads that predate it, from the
  on-disk transcripts; a thread whose transcript Claude Code has since rotated
  away is unrecoverable, as the field survives in no event payload.

- **`agentId` / `attributionAgent` join the Claude Code field ledger,** which had
  been reporting both as unmodeled drift on every subagent run — ~1200 findings a
  week, drowning the real signal the ledger exists to carry.

- **`toolEndsTurn` joins the Claude Code field ledger.** It flags the tool result
  that ended an agent's turn — in practice a StructuredOutput result the schema
  accepted — so every workflow agent given a schema was reporting one finding, at
  the time the loudest entry in the ledger. Carried, not stored: the flag is
  present exactly when the result's tool_use is StructuredOutput and the result is
  not an error, and a schema rejection (which keeps the turn going) is already
  modeled as `tool_execution_error`. Nothing is lost by not persisting it, which
  is what separates this from the `attributionAgent` case above.

- **Providers are pluggable** — a harness archive has never heard of can now be
  preserved without forking it. A provider is one `Provider` descriptor (where
  its transcripts live, how to read them, what its format looks like, how it
  presents itself) declared against a new public API at
  `thread_archive.provider`, and found either through a
  `thread_archive.providers` entry point or a `providers` entry in
  `config.json` — the former is how a provider ships, the latter how one is
  developed against a checkout. Discovery is fail-soft in both directions: a
  plugin that raises on import is logged and skipped, because a third party's
  code sitting in the ingest path must not be able to stop every other source
  from capturing. `archive providers` lists what registered. Authoring guide in
  `docs/providers.md`; `thread_archive.provider.testing` ships the golden
  harness and isolated-home fixture so a plugin gets the same drift protection
  the built-ins have.

  The built-ins go through that same API rather than a private path — the
  registry is now the single source of truth that the watcher set, importer
  dispatch, setup's source list, export-drop classification, capture-coverage's
  export-fed map and the re-parse tooling all read, replacing five hand-kept
  lists that had no way to agree with each other. Making the built-ins the API's
  first consumers is what keeps it honest: a seam they need and a plugin can't
  reach is a bug rather than a private convenience.

- **cloth is no longer built into archive** and now ships as a plugin at
  `cloth/archive-plugin/` in the thread monorepo, where the transcript format
  and the config describing it change in the same commit. Archive carries no
  cloth knowledge; existing cloth threads import and read unchanged.

- **A delegating source is validated as itself.** A harness that reuses another
  provider's parser (cloth, Cowork, Claude Science all reuse Claude Code's) was
  logging its parse drift under `claude-code` and being checked against Claude
  Code's ledger. Parse identity and provenance identity are now separate: the
  parser still reads the bundle as Claude Code, while validation resolves the
  *source's* own `ProviderConfig`. `ProviderConfig.derive()` builds one from the
  parent's, unioning ledgers so a derived provider declares only its additions
  and stays current as the parent grows. cloth's `cloth_meta` line type and its
  `cost` / `session_id` fields move out of `CLAUDE_CODE_CONFIG` accordingly —
  Claude Code's ledger is a statement about Claude Code again, able to catch
  those keys appearing there for real.

- **Whether an event stores its branch metadata is declared, not listed.** New
  `ProviderConfig.persist_branch_metadata`, separate from the existing
  `has_branching`: the latter describes the format, the former is the decision
  to persist parent links, and several formats that *can* branch reach the
  archive as linear transcripts whose order already implies the chain. A
  plugin whose source is a conversation tree now gets its tree rebuildable from
  truth by declaring it.

- **The `EventBuilder` protocol matched no working implementation** — its
  `build_events` omitted `prev_occurred_at`, which `assemble_events` always
  passes by keyword, so anything implementing the published protocol faithfully
  raised `TypeError` on its first message. Corrected.

- **Provider parser configs register before validation runs.** Config
  registration is a push into the parser island (which imports nothing from the
  rest of the package and so cannot fetch them itself), and the import path
  didn't trigger it — leaving whether a provider's config was registered
  dependent on whether something else had touched the registry first in that
  process. The failure was silent in the worst direction: the provider fell back
  to an all-permissive default, so its own known line types started reporting as
  drift while its real drift stopped being caught.

- **The built-in providers are built by the public factory they publish.** The
  three line-stream built-ins (codex, Grok, Antigravity) each called the private
  import engine directly, so `line_stream_importer` — the only line-stream path a
  plugin has — had no users inside archive and nothing that ran exercised it.
  They go through it now, and a ratchet keeps them there. Claude Code stays the
  documented exception: it merges compaction continuations and forks into
  existing threads, which the one-file-one-thread lifecycle can't express.

  Converting them found the factory's callback contract too narrow to describe
  archive's own providers. Grok's timestamps live in files *beside* the
  transcript and Antigravity namespaces message ids by session, so both need the
  path and the source id — which no callback received. `prepare` is handed both
  now and carries them on the context the later callbacks read. A provider whose
  session isn't a function of its JSONL alone could not have used the public
  factory at all before this.

  `line_stream_importer` moves to the import engine and `claude_code_line_stream`
  to the Claude Code importer, leaving `thread_archive.provider` a pure
  re-export surface — it had been the one generic module reaching into a
  specific provider's implementation. `ImportState` joins the public API: both
  db-scan built-ins annotate what `get_import_state` returns, and a plugin
  couldn't name that type.

- **One provider's display quirk no longer reshapes another's turns.** How a
  thread's stored events should be *displayed* is now declared on its provider
  as a `RenderPolicy` and resolved per thread, replacing rules that generic
  reader code applied to every source. Three of them existed, two doing real
  damage:

  Grok's `<user_query>` unwrap ran on **every** user turn from every provider,
  so a turn that merely *quoted* the wrapper — a bug report, a pasted transcript
  — was replaced by whatever sat inside the tags it happened to contain. In the
  live archive that hid a 3272-character Claude Code turn behind the 3
  characters of an example span. The payload was intact throughout; only the
  reader was lying, which is why nothing caught it.

  Codex's block-hiding rules keyed off a `codex_` block-type prefix rather than
  the thread's actual provider, so any source writing a block type under that
  prefix inherited codex's idea of what counts as machinery. A policy that
  declines a block returns `DEFAULT_VIEW` and the reader falls back to its
  default flattening, so preserved-but-unmodeled content stays visible without
  needing a provider to vouch for it.

  A `RenderPolicy` is presentation only — it never changes what was stored, and
  the untouched payload stays in the event log and the viewer's raw view.

- **Session-id shapes are declared, not assumed.** Resolving a bare session id
  to a thread used a hardcoded separator set (`:` and `-`) derived from Claude
  Code and codex and applied to every provider. Providers now declare their own
  `session_id_separators`, so a plugin composing `{workspace}|{session}` is
  resolvable by its bare session id, and a provider storing the bare id is
  matched exactly rather than by a suffix rule it never opted into. Naming the
  source narrows the shape as well as the rows: a caller that knows where a
  reference came from no longer risks a uuid resolving through a separator only
  some *other* provider composes with. Separators are LIKE-escaped alongside
  refs, so a provider may declare `_` without it acting as a wildcard.

- **Tool-call FTS documents index every input key** — the extractor used to
  index only the first "content-shaped" key of a tool's input and drop the
  rest, which lost a Write's `file_path` behind its `content` and a Bash
  `command` behind its `description`. Now every string body key (`content`,
  `command`, `query`, …) is indexed whole and every remaining key is indexed
  as `key=value` metadata (values capped at 500 chars), with metadata leading
  the document so the 2000-char cap can't truncate small keys away behind a
  large body. Requires an FTS rebuild for history (`rebuild_fts`).

- **`thread_search` groups by thread on demand** — the thread-granular list view
  an empty-query browse returns is now reachable from any keyword search, via
  two new `group` modes. `group='browse'` lists the matched *threads* alone (one
  row each: title, provider, size, when, hit count — no messages);
  `group='nested'` keeps the messages, clustered under their thread in event
  order. Both count `limit` in threads — nested caps each thread at 5 hits and
  folds the remainder into its cluster header, so one sprawling thread can't eat
  the view — and both enumerate every matched thread, standing down the
  cross-thread duplicate fold that the ranked shapes use to save an agent's
  result slots (dropping a forked thread off a list that exists to enumerate
  threads is a different thing than folding a repeated row). Asking for a list
  shape is explicit, so it outranks the suppressions that keep the ranked shape
  ungrouped under a `thread_id` scope or a structural sort; `output='count'`
  still wins, tallying per thread as before. Clustering breaks the
  rank-order-is-row-order assumption the match-quality verdict and the MCP
  widen-retry both rested on, so ranked position now rides along on each hit
  (`_rank_pos`) and both read the head through `format.top_hit`.

- **The FTS index stops storing the corpus twice** — `event_search` becomes an
  external-content FTS5 table over the `events_fts` shadow: the index holds only
  postings; column reads, snippets, and LIKE scans resolve through the shadow by
  rowid (~4GB off a ~14GB index at the current corpus, and the amplification
  stops compounding with scale). Shadow→index sync is trigger-based
  (`events_fts_ai/_ad/_au`), so every writer — incremental import, thread-meta
  sync, redaction, repair scripts — writes the shadow alone and the surfaces
  can't drift. Verify's parity pairs the shadow count with the index's own row
  ledger (the fts5 `%_docsize` shadow table) in one statement, and a new
  `fts_triggers` component reports missing sync triggers. A live index predating
  the layout is swapped for an empty current-shape table on open — lexical
  search goes dark, not wrong — and stays deliberately triggerless until
  `reindex` refills it: over a populated shadow and an empty index, a
  trigger-fired FTS 'delete' targets postings that don't exist, which fts5
  raises as SQLITE_CORRUPT (the in-place migration of the production store hit
  exactly that; the dark window is triggerless so no one hits it again). The
  conversion was validated with a golden-query capture over the production
  pipeline — 23 query shapes, results byte-identical up to order among
  exact-score ties (whose old arbiter, insertion-order rowids, no longer
  exists).

- **The semantic-search matrix is mmap'd, not resident** — the KNN serves every
  content-type scope from one full-corpus pack (`vector-pack/` beside the
  index: token-named `.npy` files, rebuilt whenever the vector store moves) via
  row masks over an `np.load(mmap_mode='r')` matrix. Per-process RAM no longer
  scales with the corpus: processes share one file-backed copy the OS can
  reclaim under pressure, where each previously pinned its own float32 matrix
  per cached scope. Same float32 bits, same scores — the golden capture diffs
  empty against the pre-pack pipeline.

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
