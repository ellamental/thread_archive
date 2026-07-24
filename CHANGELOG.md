# Changelog

## Unreleased

- **The embed cohost was the one drain that could fall behind silently.** `lag_s`
  covers lexical freshness; nothing covered the vector arm. If the cohost stalls,
  every other signal stays green — the poll loop is healthy, `lag_s` is low, searches
  return hits — and the only symptom is that the *right* hit is missing, because the
  conversation was never embedded. Each pass now records into `watch_embed_last`:
  docs embedded, wall time, docs still pending (with `capped` when the real backlog
  exceeds one batch — a stalled drain and a caught-up one both embed zero, and only
  that distinguishes them), chunks pending, and the newest embedded event's age.
  The drain already reported a `select` / `model_load` / `encode` / `write` split
  into a phase and the cohost was discarding it; `CollectingPhase` keeps those
  timings in memory without writing a ledger row, so the steady path gets the same
  breakdown a tracked load does.
- **`maintain()` is timed.** It is the interval-gated upkeep whose two halves — the
  manifest checkpoint and the shard rebalance — scale with the archive rather than
  with what just arrived, the shape that became a quadratic term once already.
  Gating bounds how often that is paid, not how much. `watch_maintain_last` records
  the total split across `checkpoint_ms` and `thread_meta_ms`, so a regression names
  its half instead of surfacing as the poll loop mysteriously slowing down.
- **Latency records carry what else was running.** Every timing so far was a bare
  duration with no way to tell a slow pipeline from a busy machine. Searches and
  reads now sample contention at the start of their work: `inflight` (concurrent
  calls in this process), `refreshing` (background matrix/graph rebuilds, which
  stream the pack off disk and run for seconds), and `wal_age_s` — seconds since
  anything last wrote the index, read off the SQLite WAL's mtime. That last one is
  the cross-process signal: reads never touch the WAL, so a fresh one means the
  watcher or an import is writing the database this search is reading, and it makes
  the retrieval ledger joinable to the ingest side with no coordination between them.
  Fields are omitted when they say nothing, so an idle-machine call records none.

- **An archive opened just after boot is registered.** The registry's per-process
  throttle read a missing entry as "last registered at monotonic 0.0". `time.monotonic`
  has no defined epoch and counts from boot on Linux, so on a machine up less than the
  five-minute interval every *first* registration was silently swallowed — an archive
  could be opened repeatedly and never become known. A missing entry now means never
  registered. Caught as two red tests on CI's fresh runners that no long-uptime dev box
  could reproduce; pinned by a test that fakes three seconds of uptime.
- **The browser suite mocks `/api/archives`.** The health view gained the registry
  fetch; the e2e API surface did not, so `/health` failed its own no-unhandled-request
  assertion — the check working exactly as designed.
- **The haystack benchmarks are archives you can open.** LoCoMo and LongMemEval are
  scored per question — each question retrieves inside its own small history — so the
  harness built a throwaway home per question and the corpus existed only as hundreds
  of fingerprint-named micro homes: not openable, not searchable, not visible anywhere
  archives are listed. `evals/haystack_corpus.py` builds each dataset as one ordinary
  home beside `homes/cdr` and `homes/swe-chat` (locomo one thread per turn, ids
  namespaced by conversation since `dia_id` restarts at `D1:1` in each; longmemeval one
  thread per haystack session, deduped across the shared pool), tracked like any load
  and tagged `benchmark`. The per-question homes stay unregistered workspace.
- **Building a snapshot is a tracked load.** A snapshot of a real corpus runs for tens
  of minutes and only the index phase — `reindex`'s own run — was recorded, so the copy
  that opens it was a silent stretch with nothing to watch and no record afterward. The
  build now writes a `snapshot` run into the home it builds, phased `copy` / `index` /
  `verify`, with the copy's file and byte counts. `reindex` still keeps its finer-grained
  record inside the index phase; the ledger holds both.
- **The vector arm's latency is now attributable.** `semantic_ms` covered five
  unrelated costs — the query embedding, the scope-mask query, serving the KNN
  matrix, the matvec, and hydration — so a 28-second observation named an arm and
  nothing else, which is precisely useless in the tail where the value is. The probe
  now reports `embed_ms` / `scope_ms` / `matrix_ms` / `knn_ms` / `hydrate_ms` nested
  inside the arm total, plus `matrix_built` for the inline pack build a process's
  first query pays. The sub-stages ride the same probe the bench reads, so
  `retrieval_gold_gate.py --latency` and the production ledger report the same split.
- **The cold-model flag was pinned true and named nothing.** `cold` was one bit OR'd
  across both model arms and sampled at entry, so an installed-but-disabled
  cross-encoder — permanently "available and not loaded" — made every search read
  cold. It is now per-arm and sampled where a load would actually be paid:
  `embed_cold` at entry (the vector arm runs on every non-structural query),
  `rerank_cold` at the re-rank itself, so an arm that sits out contributes no signal.
- **Startup cost is recorded.** `warm_models` exists to move the tens-of-seconds model
  load off the request path, and moving a cost is not removing it. Each pass now
  writes a `warm` ledger row — total plus `embed_ms` / `rerank_ms` / `graph_ms` /
  `search_ms`, and the stages that failed — so "how long after a restart is this
  server useful" has an answer, and a load creeping toward an MCP client's timeout is
  visible before it crosses.
- **Searches that raise are recorded.** Reads already logged in a `finally`; searches
  did not, so a search that failed slowly left no trace — biasing every percentile
  computed off the ledger toward the calls that happened to succeed. Both now record
  `failed`, with the time burned. Rendering is measured too, as `render_ms` beside a
  `duration_ms` that still means retrieval alone, and reads record `chars` — a read's
  cost tracks how much conversation it materialized, so latency without size was a
  distribution missing its main explanatory variable.
- **The watcher timed itself.** The pass heartbeat recorded what ingest did and never
  how long it took: no pass wall time, no per-source cost, and no freshness number at
  all. It now carries `pass_ms` / `pass_ms_max`, a per-source cumulative `ms` beside
  each source's yield counters (a source polled 1.5M times for nothing and one polled
  twice expensively were indistinguishable), and `lag_s` — how far behind real time
  the newest ingested event is, sampled only on passes that actually imported.
- **The web viewer has request telemetry.** It is a real read surface running the same
  engine, and nothing had ever recorded its latency. Served requests now append to
  `web-requests.jsonl` (path, status, size, wall time; no query strings). Deliberately
  its own file — `retrieval-usage.jsonl` is the sampling frame evals are mined from,
  and folding a human clicking around into "queries an agent asked" would bias them.

- **Bulk import was quadratic: each imported file paid for every file before it.**
  `import_path` runs the maintenance checkpoint per file, and two parts of that
  pass cost O(archive size) — the `import_state` snapshot rewrites every row (with
  two fsyncs), and the shard-rebalance sweep walks every thread file to count them.
  Per file that is invisible; across a cold catch-up or a corpus build it is a
  quadratic term, and it dominated: on a 9,146-conversation build the `import_state`
  table reached 9,146 rows and 3.4 MB, rewritten in full 9,146 times. Ingest
  throughput collapsed from ~89/s to ~8.5/s *within a single build*. Both are
  cadence work whose staleness is already documented as safe, so the maintenance
  form now runs them on a sweep interval (first call in a process always, then at
  most every 30s) while the full form — pre-backup, pre-reindex — never defers.
  A/B over 6,000 imports, one binary and one machine: 225s → 40s (5.6×), with
  throughput decay across the build falling from 6.8× to 2.2× — what remains is
  index growth, not a quadratic term. On the real CDR corpus, 3,000 conversations
  ingest in 27.0s against 71.4s. The live watcher benefits too: it was rewriting
  the whole snapshot every poll.
- **A phase now reports whether it is slowing down, not just its mean rate.** A mean
  is exactly the statistic that hides work whose per-item cost grows with what it
  has already written — the import collapse above averaged out to a healthy-looking
  15.8/s. Each phase keeps the throughput of its first and most recent 10s windows
  and reports `slowdown` (their ratio) into the live state and the ledger; the CLI
  prints it once it is material. Sampling rides the existing live-state refresh, so
  the progress path pays nothing, and a phase shorter than two windows reports no
  trend rather than a fabricated one. On the A/B above it read 6.23 against 1.56 —
  the pathology is now a number the ledger carries, not something to notice by eye.

- **Model colors in the viewer mean something now.** The per-model accent used to
  be a hash of the model's name, so `claude-opus-5` came up red while the other
  opuses were green and a thread's colors said nothing about what ran in it. Hue
  now comes from the model's family — every opus a green, every gpt/codex a blue,
  fable red, sonnet violet, haiku amber, and so on down a table of the families
  the archive actually holds — and the version picks a shade inside that family's
  band, so a thread header or a stats table reads as "two opuses and a gpt" at a
  glance. Models from unknown families keep a hashed hue, muted so they can't pass
  for a family color.

- **The drift quarantine is no longer eaten by the export-drop watcher.** Both
  live under `<home>/dumps/`, but only account exports are drops: the watcher
  scanned `dumps/drift/`, failed to classify it, and moved the whole tree into
  `dumps/failed/` as an unrecognized export — burying the preservation copy of a
  degraded source's raw store (often the only copy left once the harness prunes)
  under a name that means "this export needs a look", raising a capture error per
  sweep, and costing the snapshotter the prior generations it copies
  incrementally against, so each night re-copied the whole active window. The
  drift dir is now reserved alongside `failed/` and `imported/`.

- **A quiet archive no longer reads as a stalled watcher.** The health page ages
  the operational records against *now* — capture is stale past 15 minutes — but
  they rode the status survey's TTL cache, which nothing refreshes but a request.
  A page opened after twenty idle minutes therefore got a twenty-minute-old "last
  check" stamp and declared a perfectly live watcher stalled, red trust center and
  all. `api.operational_records` splits the freshness-bearing half of `status`
  (health records, pipeline verdict, watcher liveness, backup device check, load
  state) from the expensive counts; `/api/status` serves the counts cached and the
  records fresh. The health page re-reads on its idle cadence too, so ages stay
  true while it sits open instead of freezing at load time.

- **Registry entries can say what an archive is *for*.** Entries in
  `~/.thread/archives.json` carry an optional descriptive `role` (`live`,
  `benchmark`, `snapshot`) set via `thread_archive archives --set-role` /
  `--clear-role`, shown in the CLI listing and as a badge on the health page's
  archive cards. Purely descriptive — a role grants and gates nothing.
  `snapshot` stamps `role: snapshot` on its dest automatically. Scratch homes
  stay out of the registry entirely: a restore drill's temp home and a restore's
  staging directory no longer register (including via their smoke passes'
  re-entrant opens).

- **An abandoned CLI session no longer reads as a capture failure.** Capture
  coverage judged a source's ingest stale when its newest store *mtime* ran ahead
  of its newest archived event — so a session opened and never used (grok and
  codex write metadata at startup, before any turn) failed the nightly's coverage
  stage from the moment it was consumed until the next real conversation landed,
  and marked its source `degraded`, the verdict the MCP search notice and
  `fix-import` key on. Staleness now measures the store activity the archive has
  yet to *account for*: a file the importer consumed whole and settled as an empty
  session — one routine skip-ledger record, watermark covering its current bytes —
  is not evidence of missed capture. A session the archive keeps re-consuming to no
  effect is not settled and still fails, which is what keeps the drift catch the
  comparison exists for: a blind parser's sessions grow.
- **Loading an archive is now a tracked, phased, timed event.** Bringing an
  archive up to date — importing transcripts, rebuilding FTS, embedding vectors —
  was the longest thing the product does and the least visible: work happened
  inside one call that returned a count at the end, so "is it stuck or working",
  "how far along", and "which phase costs the hours" were unanswerable. The
  `_ops.load_runs` ledger records each load as a run of phases; a phase carries its
  wall time, a `done/total` progress counter with a live ETA, and a `detail`
  split of named sub-timings. The embed drain reports its `select`/`encode`/`write`
  split (measured: encode is ~96% of it), so where the time goes is a fact, not a
  guess. Live progress publishes to `<home>/load-state.json` for any process to
  read (`thread_archive loads`, `GET /api/loads`); a summary lands in
  `<home>/load-runs.jsonl`. A run that dies mid-phase reads as `stalled`, not
  `running`. `reindex`, `embed`, and the one-shot `watch --once` catch-up are
  tracked; the continuous daemon poll stays untracked (it keeps its pass
  heartbeat and writes no per-poll rows). `THREAD_ARCHIVE_LOAD_LOG=0` disables it.
- **Time-to-first-usable-search is import, not embed.** Lexical search is live the
  moment import finishes (FTS is trigger-maintained; the vector arm degrades in
  until vectors exist), so the number that gates a new user's first search is the
  import throughput (~16 MB/s on this hardware), not the hours-long cold embed that
  runs in the background behind it. The `watch --once` catch-up now shows a live
  per-file progress line with an ETA.
- **A registry of known archives** (`~/.thread/archives.json`): an archive becomes
  known by being opened, so `thread_archive archives` / `GET /api/archives` can
  list every home and its live load state — including archives the current process
  hasn't opened. `THREAD_ARCHIVE_REGISTRY=0` disables it.
- **The embedder loaded fp32 while the cross-encoder loaded fp16.** The dtype
  policy lived in `rerank.py` and `embed.py` never applied it, so the corpus embed
  ran at full precision — the difference between a cold embed in an hour and in
  several. The policy now lives once in `embed.py` and both model paths read it.
- **The embed drain now length-sorts within a recency window, ~halving encode.**
  The embedder pads every text in an encode batch to the longest one in it; draining
  docs in the store's natural (`event_id`) order put a 30-char user turn and a
  2048-char slice in the same batch, so most of the encode was padding (measured
  ~4/5 waste, and encode is the bulk of the embed). The drain now length-sorts the
  pending docs so each batch is length-homogeneous — measured 14.2 → 27.7 chunks/s,
  a ~2× speedup on the longest phase of a cold load. The sort is *windowed*
  (`_SORT_WINDOW` docs), not global: the drain still walks newest-window-first, so
  recent-thread semantic recall stays current and a large pass writes the newest
  docs durably before the oldest. `sort_window=0` restores the natural order.
- **The health view shows every archive's load state and history.** The registry
  and the load ledger had no surface: "which archives are loading, which are built,
  and what did past loads cost" was answerable only by reading files. The health
  page now lists every known archive with its live phase, progress, rate and ETA
  (polling while a load is in flight, backing off when idle), plus a cross-archive
  history table with each run's per-phase cost. It reads from each home's own state
  file, so a load running in another process — on an archive this one never opened —
  is visible as it happens. Each registry entry carries its own recent runs, so the
  view is one fetch rather than one per archive.
- **"Loaded" claimed something the product does not do.** Being indexed and being
  reachable are independent: `thread_search` / `thread_read` answer from the single
  archive their process was started against, so an archive can be fully built and
  answer no query. The state pill now names only the derived-data axis — `Indexed`,
  or `Untracked` when an index exists but no load was ever recorded for it, so
  completeness is unproven rather than asserted — and reachability is its own
  marker, `served` / `not served`, stated on every archive rather than inferred
  from a missing badge.
- **The embed model's cold load was billed to `encode`.** The model loads lazily
  inside the first `embed_documents` call, so the tens of seconds it takes landed
  inside the first batch's `encode` timing — on a short pass that was most of the
  reported encode, and it inflated encode's share of the embed. The drain now warms
  the model up front under its own `model_load` sub-timing.

- **Stats token and cost totals were over-counted.** Claude Code repeats one
  response's full usage object across every transcript row that response produced,
  so an event is not a request. The rollup deduplicated exactly one column —
  `cache_read_tokens`, via a `request_cache_metrics` ledger — while the fold
  directly above it summed the same duplicate rows straight into `requests`,
  `input_tokens`, `output_tokens`, `thinking_tokens`, `cost` and `cost_requests`.
  On this archive that was 31M phantom output tokens and 25k phantom requests
  (production totals on the rebuild: requests 461,397 → 436,407, input 92.55M →
  88.15M, output 306.54M → 275.71M). The ledger is now `request_metrics` and holds
  every usage figure, one canonical row per provider request, `MAX` per field so a
  duplicate arriving in a later watcher poll still collapses. `thread_metrics` is
  re-derived from it rather than accumulated into, which also makes re-folding an
  already-folded window a no-op instead of a doubling — verified bit-identical
  against a 200k-event re-fold on the live index.
- `refresh_metrics` rebuilds only the threads the folded window touched. It
  previously reran an unbounded full-table `UPDATE … SET cache_read_tokens = (
  correlated subquery)` over every `thread_metrics` row on every fold, which is the
  per-request full survey the incremental cursor exists to avoid.
- `metrics_cursor.cache_requests_ready` (a one-shot "backfilled once" bool) is now
  `projection_version`, an integer compared against `_metrics.PROJECTION_VERSION`.
  Any change to the fold that makes old sums incomparable is a bump, and the next
  refresh discards and rebuilds instead of adding to them.
- Fixed a crash opening any archive predating both metrics columns. `_ADDED_COLUMNS`
  iterates in dict order, and the `thread_metrics` fixup wrote
  `metrics_cursor.cache_requests_ready` — a column the *next* entry had not created
  yet — so `init_db` raised `no such column` and the open died. It self-healed on a
  second open (the ALTER autocommits, the fixup's DML rolls back), which disguised a
  deterministic ordering bug as a transient race. Schema provisioning no longer
  writes data at all: it provisions shape, and `refresh_metrics` owns staleness via
  `projection_version`, so the steps are order-independent by construction.
- `init_db` drops projections a newer shape superseded (`request_cache_metrics`, the
  `cache_requests_ready` column) rather than stranding them. They are disposable
  re-derivations of the event log, and a stale one left in place reads like a live one.

- New gold miner **`commit`** (`thread_archive mine commit`) and the
  `evals/swechat_corpus.py` harness that feeds it. Every existing miner
  establishes its labels by searching with the engine under test — the rerank
  judge grades a pool production search returned, the query/topic labelers sweep
  with their own reformulations through the same stack — so a systematic retrieval
  blind spot is invisible to labeler and ranker alike and can never score as a
  miss. `commit` takes its gold from **provenance** instead: a linkage file pairs
  each session with the commits it demonstrably authored, an agent reads only the
  commit (message + diff) and writes queries for it, and the linked session is the
  answer. No search runs during labeling, and since the agent never reads the
  target thread there is no vocabulary leakage either — the bias query-gen carries
  by construction. Same-repo siblings grade themselves structurally (overlapping
  files 1, disjoint 0), so the confound pool costs no tokens; only the grade-2 is
  grounded, 1/0 are proxies. Needs a corpus shipping session↔commit provenance, so
  it stays out of `mine all`.

  `swechat_corpus.py` builds that corpus from
  [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat) (public, ODC-BY,
  arXiv:2604.20779), whose transcripts are native Claude Code JSONL and so ingest
  through the shipped importer unchanged. Its value is being an **independent
  hold-out**: every other gold file is mined from one corpus by one author, and
  hold-out discipline within a corpus cannot see overfitting to it. Unlike the
  BEIR/CDR/haystack yardsticks it is domain-matched — agent session logs, not a
  third-party IR corpus. Roughly 2100 of 5851 sessions carry attributable commits,
  about half of which have retrievable commit content.

- Recent-conversation cards now include up to 200 characters from the first
  non-empty user message, so a title alone is no longer the only recognition cue.

- The ranker gained a **`bm25_weight` term** over `_lex` — the lexical arm's own
  placement of a hit (peak-normalized reciprocal rank, stamped in the pool half of
  `search`) — shipped at `100.0`. The arm's verdict previously reached the scorer
  through one channel only: FTS5 orders by bm25 but never surfaces the score, and
  `_rrf`, the feature carrying rank evidence, is computed only when the vector arm
  returns. So a lexical-only search (a `tool_name` or `types` scope, a structural
  query, an archive without embeddings) ranked on density alone — and density is
  IDF-blind and length-normalized, weighing a corpus-common term exactly like the
  rare one that discriminates and then dividing by length. Measured on BEIR scifact
  over a fixed pool, varying only the ordering: the pool's own bm25 order scores
  0.682 nDCG@10 (above the 0.665 published Anserini BM25 reference) while an
  unweighted density re-scoring of that same pool scores 0.302, pushing 54 of 332
  gold documents out of the top-200 entirely (recall@100 0.924 → 0.716) and
  dropping top-10 median document length 1496 → 835 chars.

  The shipped weight is set by the **gold files, not that benchmark**: 100 is
  where findability gains .019 MRR / .015 nDCG@10, judged .013 MRR and
  rerank-cases .052 success@10, against the recall it costs the confound-dense
  files (frustration −.048 recall@10, context-compaction −.033). A deliberate
  trade, taken on the in-domain delta. 200 buys findability another .015 nDCG@10
  for more of the same recall; past ~400 bm25's order overrides the density
  evidence the topic files lean on and they break their floors.
  `evals/experiments/no_bm25.py` races the ablation.

  The external suite, tuned against by nothing, agrees: BEIR scifact lexical
  0.302 → 0.445 (recall@100 0.716 → 0.900) and fused 0.650 → 0.658; CDR lexical
  0.101 → 0.230 (recall@100 0.250 → 0.569) and fused 0.458 → 0.492;
  LongMemEval-S 0.894 → 0.912 recall@10. The benchmark gap is deliberately left
  open — `bm25_weight` near 2000 reaches BEIR's BM25 reference and breaks five
  gold floors doing it. LoCoMo is flat (fused 0.649 → 0.653, re-rank forced on
  0.790 → 0.787) because the term is out of scale there, not inert: density is
  normalized to `density_norm_chars` but unbounded, so on a corpus whose every
  document is a sub-500-char turn (median 116) density runs several times larger
  than on archive-length text and a fixed-scale bm25 term cannot reach it. Read
  a flat number on a short-document corpus as scale, not as no effect.

- `pool_cache.FORMAT_VERSION` → 2, since cached pools now carry `_lex`. A pool
  cached by an older build would have scored the new term as zero and made a
  `--set bm25_weight=…` sweep read as having no effect.

- Search quality gained a **recall-shape tier** (`tests/test_search_recall_shape.py`)
  alongside the ordering floors. The existing tier-0 metrics (MRR, success@k, and a
  "recall@k" over golds that are mostly one thread) score which thread *wins*; they
  are satisfied by a ranker that returns one right answer, so two shapes the archive
  is actually asked for went unmeasured: "every thread that mentions X" and "the
  first / last time we discussed X". Both are now scored on two blocks added to
  `tests/quality_corpus.py`, built so their golds are true by construction rather
  than by judgment — a **nonce sentinel term** carried by exactly 24 threads and
  nothing else (past the default result window, so only `group='browse'` /
  `output='count'` can return the set, and corpus noise can never fuzz the answer),
  and a **dated series** of 12 mentions across a year whose earliest mention is
  deliberately the weakest lexical match, so a chronological scan that merely echoed
  relevance order fails. Adding 36 threads left the ordering metrics untouched
  (MRR 1.0, recall@5 1.0).

- Two tier-0 invariants were **measuring precision while reading as recall**, and are
  now two-sided. `test_focused_thread_beats_passing_mentions` asserted a decoy's rank
  only `if` it was present, so a ranker that dropped genuinely-matching threads passed
  — demotion and disappearance were indistinguishable; it now requires each decoy to
  come back. `test_quoted_phrase_excludes_scattered_words` pinned the entire result
  list to a single thread, encoding "a quoted phrase has one answer" and making any
  future fixture carrying the phrase a failure; it now names the two threads its
  mechanism is about.

- `search(sort=...)` **rejects** anything but `'oldest'` instead of silently ignoring
  it. `group` and `agents` already validated; `sort` did not, so `sort='newest'` — the
  plausible guess for "when was this last discussed" — returned relevance order, a
  wrong answer indistinguishable from a right one. There is no newest sort; the most
  recent mention is read off an enumerated result set.

- External calibration re-measured at the shipped configuration (`fusion_weight=400`,
  cross-encoder off), and the fused numbers moved a long way: BEIR scifact nDCG@10
  0.509 → 0.650, CDR 0.249 → 0.458, LoCoMo recall@10 0.621 → 0.649, and LoCoMo with
  the re-rank forced on 0.756 → 0.790. Every lexical-arm number is unchanged, the
  expected shape — `fusion_weight` moves only the fused ranking. Three findings came
  out of it:
  - The lift **generalizes**. `fusion_weight` was tuned solely against the mined gold
    files, and it lifted four third-party corpora nobody tuned against (+0.141 BEIR,
    +0.209 CDR). The external suite therefore works as an unplanned held-out set for
    gold-tuned ranking changes, and is worth scoring after a defaults change.
  - The cross-encoder is **domain-bound, not superseded**. Its lift over fusion on
    LoCoMo is +0.141 recall@10, essentially unchanged by the fusion increase, so on
    turn-level dialog the two arms are additive rather than overlapping. The "buys
    ~no MRR" verdict behind `rerank_auto=False` holds for the archive's own golds
    only. It costs 4207s against the +vectors pass's 157s over the same corpus.
  - The **lexical arm is the open problem**. At nDCG@10 0.302 it still trips
    `beir_eval`'s own `BELOW BM25 — investigate` verdict (−0.363 against the 0.665
    reference) while lexical recall@100 is 0.716 — the pool holds the right document
    and the re-scoring buries it. BM25 ranks candidate *selection* only; there is no
    bm25 term in `SearchParams`, and density/recency/content-type are inert on a
    corpus with no time axis and one content type. A `bm25_weight` ranker term is the
    experiment this points at; unmeasured so far.

- `docs/search-quality.md` rewritten to the current measurement regime. It had led
  with a click-label (`--from-log`) table as its headline metric; the measurement of
  record is the seven snapshot-bound gold files, so the doc now leads with their
  per-file MRR/success@10/recall@10/nDCG@10 and demotes the click protocol to the
  alarm it is. Also corrected: the gold gate is a deliberate run rather than a CI
  row, the cross-encoder ships off by default, coherence carries fresh `graph_eval`
  numbers at the shipped γ=0.005, the rejected PageRank-authority term is gone from
  the code rather than described as a live candidate, and a latency section covers
  the p50/p95 the speed axis now measures.

- `retrieval-gold-gate` dropped from the CI suite list (`ci.toml`). Its ~140 live
  searches with the embedding + rerank models loaded run at the edge of the 600s
  runner cap, so it timed the sweep out under load. The grounded regression floor
  is now a deliberate run — `scripts/retrieval_gold_gate.py`, alongside the tuning
  loop it already hosts — while the per-commit CI path keeps the `retrieval-gate`
  arm-liveness probes.

- The gold gate scores the speed axis too: `--latency [REPS]` measures warm
  latency over the same queries it scores for quality (pool cache OFF — the arms
  are the cost being measured) and prints the joint report, so a `--set` tuning
  decision reads on both axes at once. This is the seam the quality rebuild needs:
  the cross-encoder re-rank is both the top quality lever and the top latency, so
  re-enabling it means doing so within a budget. Speed has a fail-fast too: with
  `--fail-early`, a smoke test runs the queries that were *slowest at baseline*
  (the corpus's own pathological cases) against a p95 ceiling and bails in tens of
  seconds before the full pass. `--budget-ms` sets an absolute ceiling; the
  default is 1.5× the recorded latency baseline. `--latency-smoke` runs only that
  smoke test — a ~1-minute interactive speed check (the quick loop is
  `--cache --latency-smoke --set …`), with the full ~10-min pass kept for the
  confirm. `thread_archive._ops.speed` is
  the measurement core (warm reps, cache suspended, per-stage from the same probe
  the usage ledger records), with a `latency-runs.jsonl` timeseries and
  `latency-baseline.json` beside the quality ones. First measurement: warm search
  is FTS-dominated now that the re-rank ships off — p50 ~800ms, p95 ~1.5s, with
  code-identifier queries the slowest shape.

- Ranking-knob tuning moved onto the gold gate, which is now the interactive
  loop rather than only the CI floor. `scripts/retrieval_gold_gate.py` gained
  `--set field=value` (score a candidate `SearchParams`), `--cache` (persist the
  candidate *pools* across processes), `--fail-early` (stop once a floor is
  provably unreachable — sound, so it is safe on the CI path too),
  `--max-regressions N` (abort once N cases that used to rank stop ranking), and
  `--only`. A tuning run is flagged `overrides` in the run ledger and never
  overwrites the per-case baseline, so an experiment can't be read as the
  baseline moving. Over the full gold set a re-run at new weights is 132 s → 19 s.

- The caching that makes the above fast lives in the retrieval pipeline, not the
  eval harness: a search now splits into `retrieve_pool` (the FTS + vector +
  fusion half, which reads only the query, the structural scope, and the two
  pool-shaping knobs `rrf_k`/`pool_floor`) and the ranking half (every weight).
  `_retrieval.pool_cache` is an opt-in, contextvar-installed, fail-soft cache of
  the pool half, keyed on every pool-affecting input — production never installs
  one. `rank.score_features`/`score_from_features` split the scorer the same way,
  so a weight change re-reads one set of feature rows. `_eval.evaluate` grew an
  `early_stop` hook (and reports `scored`/`aborted`/`per_case`); the gate's
  `EvalProgress.best_possible` is what makes the fail-early bound exact.

- Fixed a wall-clock race in the gold gate's scores: the community-coherence
  re-rank reads a corpus graph built on a background thread, which lands partway
  through a scoring run, so cases before it were ranked without coherence and
  cases after it with — where the split fell depended on how fast the box was.
  Two runs of identical code could disagree, and the floors were calibrated under
  it. The gate now builds the graph inline before scoring any case, making a run
  a function of the code and the snapshot alone (verified: cached and uncached
  runs now agree on all seven gold files to the digit).

- Stats now separates cached input reads from token totals across the overview,
  provider, model, monthly, and heavy-session views. Codex's provider-native
  inclusive input count is normalized to uncached input before aggregation, so
  cache hits remain visible without inflating its comparable token total; token
  and cost amendments also rewind the derived metrics projection immediately.
  Anthropic's native `cache_read_input_tokens` spelling is normalized too, and
  Claude Code's repeated content-block rows are counted once by API message id
  through a request-level incremental projection.

- Retrieval `fusion_weight` raised 100 → 400, recovering the paraphrase recall the
  cross-encoder used to buy — at no latency cost. Term density is unbounded, so a
  short doc carrying a few of a long question's common words outscored the fusion
  term's ceiling several times over and sank the vocab-mismatch answers the vector
  arm had already ranked first: on the findability cases the semantic arm alone
  scored MRR 0.693 while the final production order scored 0.581, with 12 cases
  whose gold sat at semantic rank 1 and final rank 2–13. Weighting cross-arm
  agreement to density's working scale keeps them reachable. Every gold file
  improves on nDCG@10, six of seven on MRR: findability 0.566 → 0.666 (recall@10
  0.859 → 0.922 — past what the cross-encoder reached), suicide 0.905 → 0.929,
  rerank-cases 0.595 → 0.632, context-compaction 0.950 → 1.000, needle 0.739 →
  0.762. Head order tightens rather than flattens (success@1 0.551 → 0.609), the
  risk the previous calibration had flagged. Past ~500 the vector arm starts
  overriding lexical evidence it should defer to; saturating density instead
  (`d/(d+k)`) buys the same paraphrase recall and costs far more elsewhere, so the
  linear term stays. `evals/experiments/fusion_light.py` (the previous 100) and
  `fusion_heavy.py` (800) keep both sides of the optimum measurable.

- The retrieval gold gate now gates **all seven** mined gold files.
  `topic-cases-needle` and `topic-cases-context-compaction` were scored and printed
  on every run but carried no floor entry, so they could have regressed to zero
  without failing CI. Every floor is also recalibrated to the shipped ranking
  config on an explicit rule: scoring is deterministic — the same code over the same
  snapshot reproduces the same numbers to the digit — so headroom is a regression
  tolerance rather than a noise band, and its natural unit is one case. A floor sits
  `1/n` under its measured value, so a single case may regress and two fail the
  gate; small files therefore carry the widest absolute gaps (a 7-case topic file
  tolerates 0.143, the 64-case findability file 0.016). An existing floor is never
  lowered to accommodate a change. Several had gone stale when the `fusion_weight`
  change lifted their files at once — suicide's MRR floor moves 0.58 → 0.78 and
  judged's recall@10 0.70 → 0.85.

- The web viewer now opens as a retrieval workspace instead of an empty reader:
  a real home page searches every provider, exposes source/date facets, and groups
  recent conversations by day; the same URL-synchronized search surface serves
  home, results, and the navigation rail. A contextual app header replaces the
  corpus-index telemetry strip, `/` or Command/Ctrl-K focuses search, and the
  sidebar's competing recent-title filter is gone.

- The retrieval-usage ledger now records a **per-stage latency breakdown** for
  every MCP search: `fts_ms`, `semantic_ms`, `rerank_ms`, `did_rerank`,
  `pool_size`, and `cold` (present when a model loaded inside the request — the
  cold-model tail). Total `duration_ms` alone couldn't see which stage a slow
  search spent its time in; the breakdown makes the ledger self-diagnosing and any
  latency change self-validating. A fail-soft, opt-in `_probe.SearchProbe`
  context-local carries the timings out of `search()` — no probe installed (evals,
  tests, direct callers) means every timing point is a cheap `is None` check, so an
  unmeasured search is never slowed. Still ids and timings only, never content.

- The retrieval gold gate now **records every run as a timeseries**, not just a
  pass/fail against fixed floors. Each run appends one row to
  `<home>/gold-runs.jsonl` (`thread_archive._ops.gold_runs`): per gold file's
  MRR/success@10/recall@10/nDCG@10 and p50 latency, the active `SearchParams`, the
  model-arm switches, the snapshot id, and the code commit. So the baseline is a
  recorded history — "baseline was 0.46 MRR on commit X under pool=24, 0.44 on Y
  under pool=12" is a lookup (`retrieval_gold_gate.py --history`), and the
  before/after of a defaults change is on disk under the config that produced it
  instead of needing the old configuration re-run. The gate's *verdict* stays a
  floor check (a displayed number is not a quality score — see the gate docstring);
  the ledger is the same telemetry the gate already prints, kept.

- **Search worst-case latency brought under ~1.5s** (from a 7s p50 / 57s p99 in the
  usage ledger). Three tail sources fixed:
  - *Cross-encoder re-rank off by default* (`SearchParams.rerank_auto=False`). It
    was the pipeline's dominant cost — measured 2–4s on a long conceptual query,
    with wide variance — for ~no gold-file MRR over the fused
    lexical+semantic+coherence stack. Auto-re-rank now sits out; `rerank=True` still
    forces it, and the community-coherence re-rank still orders the head. The
    quality-rebuild seam is the search lab (`rerank_pool8.py` = a budget-fitting
    re-rank candidate, `rerank_rich.py` = the old full budget to beat). Its
    `rerank_pool` (24→12) and the new `rerank_doc_chars` (1500→768) knobs stay, for
    when a re-rank re-earns its place within budget.
  - *Code-identifier queries no longer scan the whole corpus.* A `foo_bar` query
    whose exact-phrase MATCH came up short fell through to a `content LIKE '%…%'`
    full-table scan (~10s over ~3.9M rows). Now indexed token-MATCH fallbacks (the
    identifier's tokens, which ride the FTS index) fill the pool first, and the
    residual substring scan — the within-token catcher MATCH can't do — is bounded
    to the recent-id window (`_LIKE_SCAN_CAP`). Worst case ~9.8s → ~0.2s (0.6s for a
    genuinely all-rare-token query).
  - *Cold-model load no longer lands in a request.* A query arriving before the
    server's background warm finished used to block on the tens-of-seconds model
    load (the 56–134s ledger outliers). The server now defers construction to warm
    (`model_slot.set_defer_construction`): until the models are resident, a query
    serves lexical-only (fast) and the semantic/re-rank arms rejoin automatically
    once warm lands.

  The gold gate's `findability-cases` floor is recalibrated to the reranker-off
  baseline (MRR 0.72→0.54, nDCG@10 0.75→0.60): its paraphrase-match cases are the
  one dimension the cross-encoder uniquely lifted, so it dropped when auto-re-rank
  went off (most other gold files *improved* — the cross-encoder had been shuffling
  their good heads). Recovering paraphrase recall without the cross-encoder is the
  tracked quality-rebuild that re-raises the floor; both regimes are recorded in
  `gold-runs.jsonl`.

- `SearchParams` gained `rerank_doc_chars`, the per-passage character cap the
  cross-encoder scores each hit at — first-class so the search lab can race passage
  length as a plain `PARAMS` experiment.

- `evals/search_lab.py` gained `--sample FRAC`, a fast-iteration subset for the gold
  bench: it scores a deterministic, hash-selected slice of each gold file (the same
  cases every run, nested as `FRAC` grows) instead of the whole file. Paired with
  `--only <experiment>` it turns a tuning loop from the full bench's tens of minutes
  (baseline + every experiment × all ~124 gold cases, fused + reranked over the
  snapshot) into a couple. A subset reads a *direction* on grounded data, not the
  promotion delta — the CLI prints a SAMPLED banner and per-file `n/n_full`, and the
  full bench (drop `--sample`) stays the promotion bar.

- The shared in-process model (nomic embedder, cross-encoder reranker) is now safe
  under concurrent use. `ModelSlot` grew a `use()` guard that serializes access to
  the one process model, and `embed._encode` / `rerank.rerank_scores` drive their
  forward pass through it. A torch forward pass isn't reentrant: two threads
  encoding at once corrupted the model's length-sized buffers, surfacing as
  intermittent `embed: encode failed (size of tensor a (N) must match tensor b (M))`
  and a silent fall back to a lexical-only pool for that query. It bit anything that
  fans searches out over a shared embedder — `thread_archive mine rerank --jobs 5`
  (~9 in 400 query-embeds failed), and any daemon serving concurrent searches.

- Search's community-coherence re-rank now gates on embedding availability at the
  call site: it runs only when the embed arm is live (`embed.is_available()`), so a
  core lexical install — or `THREAD_ARCHIVE_EMBED=off` — skips it instead of kicking a
  graph build that probes an `event_vectors` table that never exists (previously a
  swallowed per-query exception — fail-soft, but noisy). The graph primitives stay
  embed-agnostic for tests and direct callers; only the search path gates.

- The eval lab gained two external benchmark instruments beside `beir_eval.py`:
  `evals/cdr_eval.py` (NVIDIA ChatRAG's CDR, a shared-corpus conversational-retrieval
  benchmark scored by nDCG@10) and `evals/haystack_eval.py` (`--dataset
  locomo|longmemeval`, per-question haystack retrieval scored by recall@k against the
  datasets' published baselines). The haystack harness caches each built+embedded
  corpus home by content under `~/.cache/thread-evals/homes/`, so a re-run — or the
  `--rerank` pass over an already-embedded `--vectors` corpus — reuses the embeddings
  instead of rebuilding (`--rebuild` forces a fresh build, `--fresh` uses throwaway
  homes). Measured numbers land in `docs/search-quality.md` (External calibration):
  the full stack reaches LoCoMo recall@10 0.756, above DRAGON's 0.662 at every cutoff.

- `evals/search_lab.py` now races the `experiments/` configurations over the **snapshot-bound
  gold files**, and a bare run scores **both benches** — gold and synthetic — where it used to
  score only the synthetic corpus. The gold bench runs the fused production pipeline natively
  over the frozen snapshot's vectors and prints one leaderboard per gold file: the
  challenger-vs-baseline ΔMRR on the graded pools the gold gate floors, which is what actually
  credits a ranking change (the synthetic corpus, lexically easy, only points a direction;
  `retrieval_eval.py --cases` scores a single production config, not a challenger). Running both
  by default is the point — the synthetic leaderboard lands in seconds while the gold pass
  (real models over the whole snapshot, minutes) is still going, so a gross regression shows
  immediately and the grounded verdict follows; `--gold` / `--synthetic` narrow to one. Snapshot
  home defaults to `~/.thread/archive-snap` (`$THREAD_ARCHIVE_SNAP`), gold dir to `~/.thread/archive`
  (`$THREAD_ARCHIVE_GOLD_DIR`), reusing the gold gate's file-discovery and snapshot-fingerprint skip
  so a moved corpus is never scored against stale golds. The synthetic bench runs first (into a
  throwaway home it deletes) and gold repoints the engine off it before reading, so the synthetic
  corpus can never leak into the snapshot. Both keep coherence off so the delta stays
  deterministic. On a box with no snapshot, a bare run still prints the synthetic leaderboard and
  notes the gold skip; `--synthetic` asks for that explicitly.

- Gold mining is now a first-class product subsystem: `thread_archive mine` (package
  `thread_archive._mine`), replacing the `evals/retrieval_mine_gold.py` and `evals/topic_mine_gold.py`
  scripts (deleted). A `Miner` contract + registry backs three shapes — `thread_archive mine` lists
  the miners, `thread_archive mine <miner> [args]` runs one, `thread_archive mine all [N]` sweeps the
  ones a count alone can drive. The two existing miners ported unchanged in behavior (`query` →
  `judged-cases.jsonl`, `topic` → `topic-cases-<slug>.jsonl`), and two new cheap rungs join the ladder:
  `rerank` grades a retrieved pool with one judge pass (precision/ordering within what search
  retrieved; blind to recall by construction, but reports a `none-of-pool` recall-failure rate) and
  `querygen` generates difficulty-laddered queries for a random thread to test findability (recall,
  corpus-representative → `findability-cases.jsonl`). The agent corpus seam moved to `python -m
  thread_archive._mine tool search|read`, so mining runs from an installed wheel, not only a dev
  checkout. Output still lands under `~/.thread/archive/` with `cases`-in-name basenames, so the
  `retrieval-gold-gate` discovery and the baseline sweep pick up the new files automatically (ungated
  until a floor is calibrated). `evals/retrieval_eval.py` (the scorer) and the experiment lab stay on
  the bench.

- `thread_archive mine` now rate-limits its agent fan-out. A process-global semaphore in
  `_mine/_agent.py` caps concurrency at 5 live `claude` sessions, enforced at the single choke point
  every miner passes through (`run_claude`), so the ceiling holds regardless of a miner's `--jobs` or
  how many miners a `mine all` sweep chains. A second cap bounds total spend: any one `mine` command
  launches at most 25 agent sessions — a single miner's `--target` (and the topic miner's labeler
  count) clamp to it, and a `mine all` sweep spends 25 *in total*, split as evenly as possible across
  its runnable miners (a modest per-miner target is honored in full; only a wide sweep is trimmed). So
  a fat-fingered `--target 500` or `mine all 100` runs bounded instead of running up a bill. `--jobs`
  clamps to the concurrency ceiling (more workers would only block on the semaphore), and the list
  view footer states both caps.

- Removed the cross-encoder net-lift figure ("~2 points of success@10") from the docs
  (`_retrieval/rerank.py`, `docs/search-quality.md`) — a log-mined/title-proxy number never
  re-established on the snapshot-bound gold files that are now the measurement of record, where a
  rerank on/off ablation shows no reliable net lift (mixed by file: helps one, hurts another, neutral
  on the rest). The docs now state only the ~5× latency cost and the gating that follows from it; the
  auto-gate and strong-head stand-down code are unchanged.

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
