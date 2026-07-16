# Changelog

## Unreleased

- **Viewer: the recent list orders by last activity, not row writes.**
  `/api/threads` sorted on `threads.updated_at`, which is really the truth
  checkpoint's dirty-flag — every librarian summary bumped its thread to the
  top of the sidebar (each hourly drain re-surfaced freshly-curated old
  threads), while live ingest never moved a thread at all. The list now orders
  by the newest event's `occurred_at` (falling back to `updated_at` only for
  event-less threads), and each row's date shows that same last-activity time.

- **Viewer: subagent runs leave the recent list; an all-threads page arrives.**
  `/api/threads` now hides `thread_type='system'` (Task-tool subagent runs) by
  default alongside topics — the importer always intended them "kept out of the
  sidebar," but the list only filtered topics. A new `types=` CSV param selects
  exact thread types instead, `/api/thread-types` serves the live type census,
  and every list row carries its `thread_type`. On top of that: a `/threads`
  page ("all threads" in the sidebar nav) listing every type newest-first with
  a checkbox per type to hide it (URL-carried via `?hide=`, so filtered views
  share/back-navigate), a title filter, per-type row links (topics open their
  topic page), and a truncation note when the 500-row page fills.

- **Viewer: the search → read workflow got its first real polish pass** (from
  Sol's review in thread 3716655, trimmed to what held up). `/api/status` now
  serves a per-home TTL cache (stale-while-revalidate + prewarm at server
  start) — the survey took ~14s on the live archive and the status bar ran it
  on every page mount. The structured read carries provenance
  (`source_id`, `started_at`/`ended_at`, `event_count`) and the reader header
  shows it; every message offers a copy-permalink button built on the existing
  `?e=` deep-link ids. The sidebar's search widget grew a filters fold-out
  (available on every page): the source/date filters the backend already
  supported (new `/api/sources` feeds the source list; state lives in the URL
  so back-navigation restores it; a change applies live when a search is on
  screen, otherwise it arms for the next one),
  says "top 40 hits shown" when the page is full, and guards against stale
  responses replacing newer ones. Deliberately not done: custom in-thread find
  (browser find already auto-expands `<details>`), reduced reading modes, lazy
  block loading, and transcript pagination (unmeasured/premature).

- **The librarian has a driver: scheduled self-curation ships with the wizard.**
  New `_curation` module + `archive curate librarian|gardener` verb: each run
  gates on work left (read-only SQLite counts mirroring the review/garden
  queues), then spawns a headless `claude --print` against a generated strict
  MCP config (the archive's two servers, nothing else), bounded by a batch cap
  and hard timeout, heartbeating under `<home>/logs/` either way. The drain
  prompts (librarian: ~3+ citations + a search-first summary per thread;
  gardener: merge dupes / connect singletons / grow the hierarchy) ship as
  package data. Two new LaunchAgents (`com.thread-archive.librarian` hourly,
  `com.thread-archive.gardener` daily 05:00) via `archive daemon install
  --librarian/--gardener`, and the setup wizard now offers them as its
  curation step (skippable; `--skip-curation`; needs the `claude` CLI).
  Addresses the archive-bare review finding that "a memory that organizes
  itself" was a toolbox with no scheduled runner. Each fire also mirrors its
  heartbeat into the thread-family logs dir
  (`archive-librarian.heartbeat` / `archive-gardener.heartbeat`, same contract
  as the nightly's family heartbeat) so thread-monitor can watch the drains.
  Model and reasoning effort are per-drain settings in `config.json`
  (`curation.<kind>.model` / `.effort`; defaults `opus` / `xhigh`, empty
  effort omits the flag for CLIs that don't know it), and so is cadence
  (`curation.librarian.interval_minutes`, `curation.gardener.at` "HH:MM" —
  read at install; re-run `archive daemon install --librarian/--gardener`
  to apply).

- **Private-patch debt paid to zero: 146 → 0 (2026-07-16).** Every test that
  faked a `_private` now goes through a front door, and the
  `test_no_private_patch.py` baseline is empty. The redesigns that carried it:
  - *embed/rerank (41):* new `ModelSlot` (`_retrieval/model_slot.py`) holds
    each heavy model + its cached-failure flag as one public state object —
    tests inject a scripted model or reset via `SLOT.model`/`SLOT.load_failed`;
    the offline-pin tests build real HF cache trees under
    `HUGGINGFACE_HUB_CACHE`.
  - *setup wizard (41):* `run_setup` takes `interactive`/`ask`/`offer_*` as
    parameters; the box probes are public (`watcher_running`,
    `backup_running`, `embeddings_installed`); the launchd probe tests script
    `subprocess.run` and read real plists under a redirected `$HOME`.
  - *scripts (22):* `backfill_reconcile`/`recover_dropped_events` `run`/`main`
    take the transcript discovery as a `pairs=` parameter;
    `codex_line_model` is public (it was always the cross-module rule).
  - *exthost (11 incl. cov_watcher):* tests build the real
    `~/.claude/projects` layout under a redirected `$HOME`; `GRACE_SECONDS`
    is a public tunable and the grace test uses real timestamps.
  - *truth/ops (12):* `mirror_dir`, `append_line`, `load_thread_files`,
    `checkpoint_changed_threads` are public seams (fault/interleave injection
    at real phase boundaries); `MAX_OPEN_HANDLES`, `MAX_BYTES`,
    `FLAT_MAX`, `MIRROR_DELETE_FLOOR`/`_MAX_FRACTION` are public tunables;
    the nightly notify test runs a real loopback `/api/notify` and asserts
    the wire payload.
  - *misc (19):* `THREAD_ARCHIVE_NO_THROTTLE` env skips the CLI self-renice
    (one-way in-process — the suite sets it); the MCP lazy-ingest throttle
    state lives in a public `IngestThrottle` (`server.INGEST`); the mcp test
    uses the existing `THREAD_ARCHIVE_MCP_INGEST=0` kill-switch; the
    subjects fail-soft test injects a booming session through the existing
    `session=` parameter; the web limit-clamp test drives the route over
    real data; the cursor double-failure test breaks the store
    (`get_session`) instead of faking the stub.

- **The gardener returns (2026-07-16).** The old monorepo's gardener — the
  librarian's structural complement — is back as a lite diagnostics layer over
  the standalone graph: `_knowledge/garden.py` computes per-kind issue queues
  (`singleton`, `uncited`, `unparented`, `dupes` — near-duplicate titles by
  token Jaccard) plus a `garden_status` dashboard, and `graph.get_communities`
  maps the clusters. Exposed on the librarian MCP server as `garden_status` /
  `garden_queue` / `communities`; drained by the monorepo's `/gardener-lite`
  skill on a daily thread-cron cadence. The hierarchy edge vocabulary
  (`part-of`/`contains`) now lives in `garden.py`; the web viewer's topic tree
  imports it from there.

- **Viewer navigation is real links (2026-07-16).** Topic rows, tree titles,
  hierarchy/peer chips, link and citation rows, search hits, and the sidebar rail
  were `<button onClick={navigate}>` — no `href`, so no middle-click, cmd-click,
  or copy-link. All of them are now router `<Link>` anchors; every topic and
  thread row deep-links to its `/topic/:id` / `/archive/:id` URL.

- **A new house ratchet: no new private-target patches (2026-07-16).**
  `tests/meta/test_no_private_patch.py` counts every test patch of an
  underscore-private — `monkeypatch.setattr(mod, "_helper", ...)` or
  `patch.object(mod, "_helper", ...)` — and freezes the count
  behind a shrink-only per-file baseline (146 call-sites frozen here).
  Faking a module's own private couples the test to the implementation's
  internal shape and leaves the real code unexercised; the paydown is
  exercising the public surface over real data, or promoting the seam to a
  public knob and injecting through it.

- **The parallel backfill driver is gone (2026-07-16).**
  `scripts/librarian_backfill.py` and the lease-claim layer that existed only for
  it (`_knowledge/_claims.py`, the `$THREAD_ARCHIVE_LIBRARIAN_WORKER`
  claim-on-read path and `exclude_ids` in `review_queue`, the
  `.librarian-claims.json` file) are removed. The queue is state-free — done =
  the thread carries a citation + summary — so serial `/librarian` runs (or the
  host's hourly drain) stop and resume cleanly without worker coordination; the
  claim machinery had no other consumer.

- **The knowledge graph reads back out through the agent surfaces (2026-07-16).**
  The curation layer was write-only for agents: no way to list a topic's citations,
  read a topic as anything but a stub, or scope a search to a topic. Three additions,
  all over a new `_knowledge/read.py` library layer (`topic_get` / `topic_members` /
  `topic_thread_ids`): the librarian MCP grows `topic_get` (one topic with links,
  counts, graph metadata, community peers) and `topic_members` (the live citations
  with quotes, each `event_id` openable via `thread_read around_event`);
  `thread_read` on a topic thread now renders the curated page — description, links,
  citations grouped by thread with quotes (capped at 100 citations / 500 chars per
  quote) — instead of the "topic threads don't have messages" stub; and
  `thread_search` accepts `topic_id`, scoping both retrieval arms (FTS + vectors) to
  the topic's member conversations (threads cited under it or linked to it). A topic
  scope is a deliberate scope, so like `thread_id` it bypasses the
  `exclude_from_search` blacklist; an empty or bogus topic matches nothing.

- **Hooks that fired are visible in the thread viewer (2026-07-16).** The importer
  already preserved every Claude Code hook record, but the read layer hid all of
  them: `hook_additional_context` attachments rendered as a dead
  `[attachment: …]` placeholder, and `hook_context` sidecar events plus
  `hook_progress` firings were skipped outright. The structured (web) read now
  renders three block kinds: `hook` — a hook that injected content (the
  attachment's `hookName` + exactly what it injected, e.g. a UserPromptSubmit
  system-map note; sidecar `hook_context` lines surface the same way), shown
  regardless of the tools toggle since injections are conversation context;
  `hook_fired` — a bare hook_progress marker, merged client-side into one compact
  chip row per run (`PreToolUse:Read ×2 · PostToolUse:Read`), behind the tools
  toggle; and `attachment` — every other preserved attachment (todo reminders,
  skill/tool listing deltas, …) now folding open to its real content instead of
  the placeholder, also behind the tools toggle. The string path (`thread_read`
  mode=full) renders `[hook: <name>] <content>` (capped) and keeps other
  attachments label-only; chat mode hides all of it, and sidecar/progress records
  stay off the string path entirely.

- **The web viewer reads the topic graph (2026-07-16).** Topics had no web surface —
  the viewer's thread rail explicitly filtered them out and the graph was reachable
  only through the library/MCP. Two new read routes on the cohosted server
  (`/api/topics`: live topics ranked by pagerank with link/citation counts and
  community ids, plus the graph survey; `/api/topic/<id>`: one topic with its
  description/summary, both link directions labeled with the other endpoint's type,
  un-archived citations, and community peers) and two SPA views (`/topics`: the list
  grouped by community, anchored on each community's highest-pagerank member, with
  client-side filtering; `/topic/<id>`: the detail page — citation rows deep-link to
  the cited message via the reader's `?e=` anchor, topic links route to `/topic/`,
  conversation links to `/archive/`). The sidebar grows a `topics` nav link.
  Archived topics stay readable by id (merged-away topics remain referenced from kg
  history) but are hidden from the list and carry no graph metadata.

- **The topic viewer grows a hierarchy view (2026-07-16).** The old backend's wiki
  had a curated ontology tree (`knowledge_nodes`); the migration deliberately kept
  one id space (topics are threads) and dropped the tables, leaving hierarchy only
  as sparse `part-of`/`contains` links nothing consumed. Now consumed: a
  `/api/topics/tree` route derives the forest from those links (child→parent =
  `part-of`, parent→child = `contains`; roots are parents that are no one's child,
  multi-parent children appear under each parent, cycles are cut at the edge that
  would revisit an ancestor, edges to conversations or archived topics never shape
  the tree), the `/topics` page gets a communities ↔ hierarchy switch (`?view=tree`)
  rendering it as a collapsible tree, and the topic page shows a part-of/contains
  strip above the description. The hierarchy is exactly as curated as the links are
  — growing it is librarian/garden process work, deliberately not a bulk
  classification pass.

- **The librarian writes stored summaries again (2026-07-16).** Every summary in the
  index was legacy data (0 of 318 July conversations had one; the columns had no
  writer anywhere in the product — the librarian had been narrowed to citations-only
  back when nothing read summaries), yet three surfaces leaned on them: the default
  search scope (user+title+summary), `thread_read summary='short'/'indexed'`, and the
  embedding pools — so search was quietly two-tier, old threads findable by summary
  vocabulary and new ones not. Summarization is folded back into the librarian's
  per-thread pass (one read serves both outputs): `thread_set_summary` on the
  librarian MCP (short searchable summary + optional event-anchored
  `indexed_summary`; overwrite semantics, caps, topics refused), `review_queue`
  redefined as *done = cited AND summarized* — which also re-queues the ~450
  already-cited threads that lack summaries — plus a quiet window (threads that
  ingested events in the last hour are held back, so live sessions aren't curated
  mid-flight), and the one-thread-at-a-time gate now requires both halves before the
  next thread opens. Deliberately NOT kg events: a summary is thread metadata like
  the title — durable via the thread's latest-wins truth record, immediately
  searchable via `index_thread_meta` (the embed cohost re-embeds it) — which keeps
  summary text out of `kg_events.jsonl` and leaves redaction's existing thread-meta
  scrub covering every copy.

- **Retrieval usage is ledgered (2026-07-16).** The MCP surface now records
  every `thread_search` / `thread_read` to `<home>/retrieval-usage.jsonl` —
  query text, filter params, hit count, top result ids, and which thread got
  read — ids only, never content, so redaction never touches it. This is the
  observed ground truth future retrieval evals join over (search → subsequent
  read), replacing proxy-only quality measurement; the auto-titles CI gate is
  unchanged. Fail-soft, size-rotated, `THREAD_ARCHIVE_USAGE_LOG=0` disables.

- **Capture-coverage signals stop crying wolf (2026-07-15).** Three status
  signals over-alarmed or lingered, training the operator to ignore coverage.
  (1) `watch_errors_last` was failure-only — written on a poll error, retired by
  nothing — so a fault from a previous daemon run kept painting `archive status`
  red under a heartbeat that said the daemon had restarted clean hours ago. The
  daemon now clears it on the first clean pass of a run (`clear_health`, the
  green counterpart to `record_health`), scoped so a record the current run
  actually wrote is kept, not wiped. (2) Routine empty-session skips
  (`no_importable_content` — a brand-new session with nothing importable, which
  fires constantly) no longer trip the capture-skips warning; they stay in the
  ledger and its recent tally for the re-import audit, but the warning fires only
  on substantive skips (`recent_substantive`). (3) The validation-drift warning
  is unchanged in code, but the ledger's pre-fix self-noise (354 records flagging
  `attachment` / `model_change` / `unknown_line`, all now in the claude-code
  parser's expected block types) was pruned to `validation-drift.jsonl.superseded`
  so the week-long warning tail cleared at once.

- **Backups carry a full recovery bundle (2026-07-15).** A backup destination
  restored the conversations but not the install: `config.json` (source
  opt-outs), `keyring.json` (redaction keys — disk loss meant accidental
  crypto-erasure of every active redaction), the retained original exports
  (`dumps/imported/`), and the health/ledger history all lived only on the
  primary disk. `archive backup` now syncs them into a reserved
  `<dest>/.recovery/` subtree; `archive restore` installs config + keyring +
  retained exports into the recovered home (health/ledger snapshots stay at the
  mirror as reference — a restored home doesn't claim the source install's
  history); `archive restore-drill` reports what the bundle holds. The bundle is
  head-only — never snapshotted into `.generations` — so `redact --forget`
  propagates crypto-erasure to the backup on the next run. Operators who escrow
  keys off-machine can keep backups ciphertext-only with
  `{"backup": {"include_keyring": false}}`. (External whole-tree copies — e.g.
  lab's rotation slots — retain the keyring on their own schedule, the same way
  they already retain pre-redaction plaintext.)

- **Repair-dump privacy incident purged (2026-07-15).** Three live-repair dumps
  (real transcript/tool payloads, ~450 KB) were tracked in git, public on GitHub
  since 2026-06-25, and two rode the v0.0.2 wheel/sdist (briefly on PyPI). The
  GitHub repo was made private, history rewritten (`git filter-repo`) to drop the
  files from every commit and tag, then the repo deleted and recreated so
  GitHub-side unreachable objects and GH-Archive-published SHAs resolve to
  nothing. No credentials were in the payloads. `host/repair-dumps/` is now
  gitignored and a meta test fails CI if any dump-marked data file is ever
  tracked; the migration tests build synthetic plans instead of reading the real
  ones (which also un-reds CI on checkouts without the local dumps).

- **Backup destinations are owner-only (2026-07-15).** The live home is
  0700/0600 but mirrors were world-readable (0755 dirs / 0644 files — >240k
  files across the local mirror, rotation slots, and the second local dest).
  `archive backup` now enforces 0700 on the destination root and every dir it
  creates and 0600 on every file it publishes (best-effort on network
  filesystems, where modes are the share's problem); existing trees were
  remediated in place. Lab's slot-rotation script runs under `umask 077`.

- **Coverage warns on ledger volume and ages grok's export channel (2026-07-15).**
  Recent capture-skip / validation-drift records were stored but affected
  nothing; they now surface as coverage warnings. xAI account exports share
  `source='grok'` with the CLI watcher, so fresh CLI events masked export
  staleness — the export channel (threads with `source_metadata.surface='web'`)
  is now aged by itself, same 45d window as claude.ai/ChatGPT.

- **Embedding model revision pinned (2026-07-15).** The nomic loader executes
  repo-hosted code (`trust_remote_code`); the default model now loads a fixed
  upstream snapshot instead of whatever revision upstream points at
  (`THREAD_ARCHIVE_EMBED_REVISION` overrides).

- **Export redrops now merge grown conversations (2026-07-15).** The drop watcher
  imported account exports without `force`, so a conversation that gained messages
  since the last export was skipped outright — a recurring ChatGPT/claude.ai/xAI
  export captured new conversations but silently missed new messages in old ones.
  The watcher now forces the import; event-level dedup keeps unchanged
  conversations at zero new events, and a grown one gains exactly its tail in the
  same thread.

- **`archive restore` exists (2026-07-15).** Recovery was a drill plus a by-hand
  procedure (copy truth, pick a generation, make a home, reindex). `archive
  restore <mirror> --to <home>` now does the real thing: preflight (mirror scan,
  parse-error refusal, non-empty-target refusal), staged rebuild on the target's
  filesystem, count + smoke verification, then atomic publication — a replaced
  home is set aside as `<home>.damaged-<stamp>`, never deleted.
  `--generation`/`--list-generations` select a retained pre-run snapshot.

- **Topical ranking arm deleted (2026-07-15).** The topic-bridged recall arm
  (`_retrieval/topical.py`, its RRF fusion seam, `_topical` ranking term, and
  the `THREAD_ARCHIVE_TOPICAL*` flags) is gone — it was off by default with no
  measured lift, and ranking was never the graph's point. The subjects lens is
  unchanged: topic links still orient reads; they no longer reorder search.

- **`thread_archive status` tells the same truth as the operator status
  (2026-07-15).** The setup-facing status now shows the nightly pipeline's
  verdict (a red offsite run was invisible behind a green ad-hoc backup line),
  counts topic threads separately instead of inflating "conversations", and the
  operator `archive status` prints coverage warnings even when the check is
  green (a 127-day-stale export hid behind `coverage: ok`).

- **Search hits deep-link to their event (2026-07-15).** A viewer search-hit
  click opened the thread at the top; it now lands on the matching turn,
  scrolled into view and accented (`?e=<event_id>`; structured messages carry
  their source `event_ids`). A hit whose content the current toggles hide falls
  back to the nearest visible turn.

- **Privacy hardening (2026-07-15).** The archive home and truth dir are created
  and self-healed to `0700` (the live home was `755` with world-readable JSONL),
  and the MCP HTTP server refuses a non-loopback `--host` without
  `THREAD_ARCHIVE_MCP_NONLOCAL=1` — the same guard, and the same reason, as the
  web viewer.

- **Usage-mined golden retrieval eval removed (2026-07-15).** The 599-case golden
  set (search→read pairs mined by `golden_from_usage.py`), the miner, its tests,
  and `retrieval_eval.py --golden` are gone: the mined pairs assumed a
  click-on-a-result usage pattern that isn't how the archive's results actually
  get used, so the numbers weren't meaningful. The CI retrieval gate is unchanged —
  it runs the `--auto-titles` protocol, which never used the golden set.

- **The format-drift alarm detects drift again (2026-07-15).** Every recent
  validation-drift record (315/315 over the prior week) was the claude-code parser
  flagging its own deliberate preservation block types (`unknown_line`, `attachment`,
  `model_change`) — self-noise that buried any real signal. Those types are now
  registered as expected, and the real signal got sharper: an `unknown_line` block
  whose *line kind* isn't one the parser knowingly preserves (`last-prompt`,
  `ai-title`, `custom-title`, `mode`, `file-history-delta`) files a finding naming
  the new line type specifically.

- **`archive status` shows the nightly pipeline's verdict (2026-07-15).** A red
  nightly (e.g. the offsite stage failing on a TCC denial) was invisible in status
  while an ad-hoc same-disk mirror showed "backup: ok" — the one line that says
  whether the archive survives disk loss lived only in health.json. Status now
  prints `nightly: ok/FAILED (stages) → dest`. The same-filesystem backup warning
  is also rethought: offsite is opt-in and the archive can't assume it knows the
  machine's whole posture, so the shouty WARNING is now a factual note — and when
  the disk is covered by a detectable external backup (Time Machine, via `tmutil`),
  the note says so instead of implying the archive is unprotected.

- **Coverage warns when an account-export source goes stale (2026-07-15).** claude.ai
  and ChatGPT reach the archive only via manual exports; nothing nudged when the
  last drop aged out (chatgpt sat 127 days stale, silently). `archive coverage` now
  warns (never red) when an export-fed source's newest event exceeds 45 days;
  disabling the source in config.json silences it.

- **Store write timeout raised 60s → 300s (2026-07-15).** Bulk maintenance
  (full-corpus FTS rebuilds, repair/backfill migrations) holds the SQLite write
  lock for several minutes; 60s was undersized for exactly that case and errored
  35 watcher imports into health during the 07-12 migration window.

- **Reindex survives an archive that mixes full and event-only thread files
  (2026-07-15).** `reindex` bulk-loads each batch of thread records with one
  `INSERT OR REPLACE` executemany, which SQLAlchemy compiles from the first row's
  keys and binds every row against. A synthesized minimal thread stub
  (`{id, name}` — standing in for a thread file whose `type:thread` metadata line
  was lost to a crash between an import commit and its checkpoint) carries a
  different key-set than a full record, so a batch holding both raised
  `StatementError` and aborted the whole rebuild. Rows are now grouped by key-set
  so each executemany is homogeneous — which also covers records written under a
  since-changed schema landing next to current ones.

- **The watcher daemon converges to ingest ownership even after a lost startup race
  (2026-07-15).** The daemon takes the ingest-owner lock so lazy catch-up passes degrade
  to no-op probes while it's alive. It used to try exactly once at startup and, if a
  transient holder (a lazy pass mid-flight) had it, run lockless for its whole life —
  breaking that guarantee and letting lazy passes run concurrently. It now re-attempts
  the lock each poll until it holds it, then keeps it for its lifetime. (Fixes a flaky
  `test_daemon_run_holds_owner_lock_for_its_lifetime`, which raced the daemon for the
  lock at startup.)

- **Test coverage raised past 90% (2026-07-15).** Branch coverage of the package
  rose from ~81% to 96%, and the per-package `coverage_gate.py` floors were lifted
  to match (every package now floored at ≥90%). New suites cover the CLI verb→api
  dispatch surface, the setup wizard + LaunchAgent lifecycle, the semantic-search
  layer (vectors/embed/rerank, with the models faked so no torch loads), the
  grok/codex/cursor/exports/opencode importers and the vendored
  claude-code/chatgpt/claude export parsers, the watcher poll loop, the
  backfill/recover migration scripts, and the durability-kit edge branches
  (verify/redact/backup/rebuild/drain/repair).

- **Account exports are never deleted, and import loses less (2026-07-15).** The drop
  watcher used to delete an export whenever it processed ≥1 conversation — even when
  some conversations errored, and even when normalization silently dropped
  attachments/images/branch structure the parser didn't carry. A dropped export now
  moves into `dumps/imported/<kind>/` on a fully clean import — kept as the recovery
  copy, bounded to the most recent per kind (a full re-export supersedes the last, so
  the pile can't grow) — and into `dumps/failed/` when any conversation errored
  (preserved as stubs, but the export needs a look). The original download is the last
  copy of anything normalization drops, so it survives regardless. Alongside,
  three import-fidelity gaps are closed: ChatGPT `image_asset_pointer` parts are
  preserved (an image-only turn kept its pointer in truth instead of vanishing whole);
  ChatGPT's conversation *tree* — each event's `branch.parent_id` and an
  `active_path: false` marker on off-active-path (regenerated) replies — reaches truth
  so branches are reconstructable; and claude.ai `attachments` (with their extracted
  text) + `files` uploads are kept as `content_block` events instead of being ignored.
  New end-to-end goldens (`tests/goldens/providers/chatgpt-web.json`, `claude-web.json`)
  lock all three.

## 0.0.3 — 2026-07-15

Distribution moves to clone-install (the brief PyPI/Homebrew run is retired): `pip install -e .` from a clone, or `pip
install git+<repo-url>@vX.Y.Z`. Releases are annotated `vX.Y.Z` tags cut per `docs/releasing.md`.

- `archive redact`: crypto-shredding redaction (reversible / escrowed / forgotten) that removes content from every store
  without breaking the truth log's shape, verify, or dedup.
- Setup schedules the nightly backup (`com.thread-archive.backup` LaunchAgent; `archive daemon install --backup`).
- One shared MCP server: `archive-mcp --http` behind `com.thread-archive.mcp` replaces a ~3 GB resident model per stdio
  client; standalone stdio lazy-loads.
- Search ~5× faster at identical results; retrieval quality CI-gated (MRR/recall floors) over a 599-case usage-mined
  golden set; the topic graph is demoted to a `subjects:` lens (topical RRF arm off by default — no replicable lift).
- Live-capture assistant text indexed and readable (402 threads backfilled); Codex turns attributed to the real serving
  model (931 repaired in place).
- Capture blind-spot detection: skip ledger, watch-pass heartbeat, `archive coverage` nightly reconciliation; ChatGPT
  account exports import, and zero-yield imports are quarantined instead of deleted.
- SMB backup destinations work; TCC denials are named in nightly errors; ingest rides out FTS rebuilds (60s
  busy_timeout) and reindex swaps; one session-id resolver serves MCP reader and viewer.
- Hardening and reshaping: viewer generic 500s + loopback bind guard, suite isolation + `tests/meta/` ratchets, parser
  damage behavior pinned, truth module split into `_truth/`, durability kit to `_ops/`, mypy gate.

## 0.0.2 — 2026-07-11

First release published to PyPI (since retired — see 0.0.3).

- `thread_archive`: first-run setup wizard (discovers stores, imports with narration, wires watcher + MCP) and a status
  view; choices persist in `config.json` and every ingest path honors them.
- Zero-daemon path: `archive-mcp` cohosts lazy catch-up ingest under an ingest-owner flock; `archive daemon
  install|uninstall|restart|status` manages the launchd watcher from the package.
- Public API narrowed to exactly two things — the retrieval MCP tools and the versioned on-disk truth format
  (`docs/format.md`; readers refuse a newer version) — retrieval CLI verbs removed, the Python API privatized, all
  ratchet-pinned.
- Nightly publishes a verdict: a proven-fixed stage retires its own failure, tier-aware so a cheap green can't launder
  an expensive red.
- Verify hardening: quick_check on a fresh connection (kills a false "malformed index" alarm), cross-store payload
  parity, the kg log in the daily tier, failure evidence persisted with named causes.
- Integrity/ingest hardening: mirror copies under the truth-write mutex, rebuild/repair content pre-flights, a content-
  hash cursor rewind closing three silent-loss paths, per-item DB-scan failures surfaced to health.
- Boundary test lanes: package lane (wheel into a clean venv, driven over MCP stdio), multiprocess durability with
  SIGKILLed writers, provider goldens, frontend component tests, per-package coverage floors.
- Packaging: version single-sourced from `__version__`, sdist contents pinned, vendored parsers privatized under
  `_thread_import`, the family-manifest writer moved to `host/`.

## 0.0.1 — 2026-07-11

- Retrieval correctness pass: the semantic arm sits out scoped/count/oldest searches, exclusions narrow the KNN scope,
  chunk pooling precedes the top-k cut, a bm25 OR-fallback tier serves conversational queries, `sort='oldest'` scans
  chronologically.
- Crash-safe truth appends: drain intent journal, all-or-nothing batches, torn-tail repair, exclusive cross-process
  write locking, dedup enforced as a DB constraint.
- Reindex fails closed (regression gates + integrity checks before the swap); backup hardened (atomic publishes, shrink
  guard, restore drills); `archive nightly` runs backup → verify → drill with a monitored heartbeat.
- `verify` grew tiers (shallow daily parity, `--deep`, `--hashes`, `--backup`, schema parity); `archive repair`
  quarantines unparseable lines and restores committed content — the sanctioned path from red back to green.
- One-time duplicate repair: ~27k duplicated turns collapsed, ~237k dedup keys backfilled, rebuilds reproduce the repair
  instead of undoing it.
- Retrieval overhaul: titles/summaries indexed as searchable docs, long-document chunking, identifier-recall fixes,
  auto-widening MCP search scope, an eval harness (MRR / recall@k).
- Test suite pinned model-free (~2 min → ~8 s); coverage measured on every CI sweep.

## 0.0.0 — initial release

- Serverless local archive over `~/.thread/archive`: append-only JSONL truth
  plus a rebuildable SQLite index.
- Multi-provider importers: Claude Code, Cursor, OpenCode, ChatGPT/Anthropic
  exports, Grok, cloth, and friends.
- Watcher daemon (`archive watch`) tails harness stores and ingests
  continuously.
- Retrieval MCP server: `thread_search` (FTS + optional semantic/rerank arms)
  and `thread_read`.
- Librarian MCP: topics, citations, thread links, and a review queue for
  knowledge curation.
- Read-only web viewer cohosted at `:8787` (`archive watch --web`).
- CLI: import/export, reindex, checkpoint, backup, verify.
