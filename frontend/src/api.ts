// The viewer's data layer: typed fetches against the JSON endpoints the cohosted
// stdlib server (`archive watch --web`) exposes. No client framework state — plain fetch.

export interface Status {
  threads: number
  events: number
  topics: number
  links: number
  fts_indexed: number
  vectors_indexed: number
  home: string
  truth_dir: string
  index_path: string
  last_checkpoint_at: string | null
  last_verify: VerifyRecord | null
  last_backup: BackupRecord | null
  last_restore_drill: RestoreDrillRecord | null
  last_nightly: NightlyRecord | null
  last_watch_errors: WatchErrorRecord | null
  last_watch_pass: WatchPassRecord | null
  last_coverage: CoverageRecord | null
  last_source_mirror: SourceMirrorRecord | null
  last_self_update: SelfUpdateRecord | null
  // When each provider last had anything imported, all-time — unlike the watch
  // pass's counters, which reset with the capture process.
  source_last_import: Record<string, string>
  pipeline: PipelineVerdict
  watch_process_alive: boolean
  backup_same_device: boolean | null
  libraries: LibraryEntry[]
}

// One capability behind search and the library that provides it. 'off' is a feature
// this install doesn't have, which is a choice. 'degraded' is a feature it does have,
// running on a lesser substitute — search still answers, at lower quality, so nothing
// else on the page would show it.
export interface LibraryEntry {
  name: string
  tier: 'base' | 'extra'
  capability: string
  installed: boolean
  state: 'ok' | 'degraded' | 'off'
  detail: string
}

export interface HealthRecord {
  at: string
  ok?: boolean
}

export interface VerifyRecord extends HealthRecord {
  ok: boolean
  deep?: boolean
  hashes?: boolean
  failed?: number
  parse_errors?: number
  drift_events?: number
  drift_threads?: number
}

export interface BackupRecord extends HealthRecord {
  ok: boolean
  dest?: string
  verify_ok?: boolean
  mirror_complete?: boolean
  files_copied?: number
  keyring_in_bundle?: boolean
}

export interface RestoreDrillRecord extends HealthRecord {
  ok: boolean
  dest?: string
  events?: number
  coverage?: number
  seconds?: number
}

export interface NightlyRecord extends HealthRecord {
  ok: boolean
  dest?: string
  failed_stages?: string[]
  deep?: boolean
  hashes?: boolean
  drill?: boolean
}

export interface WatchSourceRecord {
  checked: number
  items: number
  events: number
  lines: number
  parse_errors: number
  errors: number
}

export interface WatchPassRecord extends HealthRecord {
  pid?: number
  started_at?: string
  passes?: number
  sources?: Record<string, WatchSourceRecord>
}

export interface WatchErrorRecord extends HealthRecord {
  count_since_start?: number
  errors?: string[]
}

export interface CoverageRecord extends HealthRecord {
  ok: boolean
  sources_checked?: number
  failed?: string[]
  warnings?: string[]
  degraded?: Record<string, unknown>
  skips_recent?: number
  drift_recent?: number
}

export interface SourceMirrorRecord extends HealthRecord {
  ok: boolean
  copied?: number
  files?: number
  bytes_out?: number
  errors?: number
  unsupported?: string[]
}

export interface SelfUpdateRecord extends HealthRecord {
  ok: boolean
  action?: 'updated' | 'update' | 'up-to-date' | 'blocked' | 'unavailable' | string
  current?: string
  target?: string
  reason?: string
}

export interface PipelineVerdict {
  ran: boolean
  ok: boolean
  failed_stages: string[]
  recovered_stages: string[]
  tolerated_stages: string[]
  nightly_at: string | null
  dest: string | null
}

// ── the action queue ────────────────────────────────────────────────────────
/** One condition the archive is asking someone to act on, with its remedy.
 *
 *  Built by the server (`_ops/notices.py`), not here: the judgment over the
 *  health records has one implementation, and a silence has to be honored by
 *  every surface that shows notices, not just this page.
 *
 *  `tone` is what the notice asks for — 'bad' is a hole in the archive's
 *  protection, 'warn' costs quality or durability margin, 'good' is available
 *  maintenance. `key` addresses the notice for silencing; `silenced_at` is set
 *  only on notices in the silenced list.
 */
export interface Notice {
  key: string
  tone: 'bad' | 'warn' | 'good'
  title: string
  detail: string
  command: string | null
  fingerprint: string
  silenced_at?: string | null
}

/** The queue split by what the operator has put aside. A silenced notice is
 *  still a live condition — it is listed, not dropped, so the count of hidden
 *  warnings is always visible. */
export interface NoticeBoard {
  active: Notice[]
  silenced: Notice[]
}

// ── archive loading ─────────────────────────────────────────────────────────
// One phase of a load (import, truth, fts, embed, vector-cache): its wall time,
// how far it got, and the named sub-timings that say where the time went.
export interface LoadPhase {
  name: string
  done: number
  total: number | null
  elapsed_s: number
  rate_per_s: number | null
  eta_s: number | null
  detail_s?: Record<string, number>
  counts?: Record<string, number>
}

// A load of the archive — live (status 'running') or finished. `stalled` means
// the process that was writing it is gone, so the progress will never advance.
export interface LoadRun {
  kind: string
  home?: string
  pid?: number
  status: 'running' | 'ok' | 'failed' | 'stalled' | string
  started_at?: string
  at?: string
  elapsed_s?: number
  duration_s?: number
  error?: string | null
  phase?: string | null
  phases?: LoadPhase[]
}

/** How loading this archive is going: the record an in-flight load publishes as
 *  it works (`current`, null when nothing is loading) plus the finished runs. */
export interface LoadStatus {
  home: string
  current: LoadRun | null
  recent: LoadRun[]
}

/** One top-level entry in the archive home, sized and sorted into what a reader
 *  can do about it. `kind` is the actionable axis: 'truth' is irreplaceable,
 *  'index' rebuilds from truth, 'sources' is raw provider material the archive
 *  deliberately never prunes, 'other' is everything that accumulates unlabelled. */
export interface DiskEntry {
  name: string
  bytes: number
  kind: 'truth' | 'index' | 'sources' | 'other'
}

export interface DiskUsage {
  home: string
  total_bytes: number
  files: number
  kinds: Record<DiskEntry['kind'], number>
  rebuildable_bytes: number
  entries: DiskEntry[]
  // Truth or index dirs resolved outside the home (both have env overrides);
  // counted in the total, listed here because they are not where a reader looks.
  external: string[]
}

// --- retrieval health -------------------------------------------------------
// Latency is reported as percentiles, never as an average: the distribution has a
// long tail (a cold process, a browse walk over a deep pool), and a mean over it
// describes no search anyone actually ran.

export interface LatencyBand {
  n: number
  p50: number
  p90: number
  p99?: number
}

/** How the window is sliced. `hour` for short windows, `day` beyond three days. */
export type Bucket = 'hour' | 'day'

/** One bucket of served searches. `at` is its UTC start — `2026-07-26` for a day,
 *  `2026-07-26T14` for an hour. `warm`/`cold` are separate because a process's
 *  first search runs an order of magnitude slower than its thousandth; `unknown`
 *  is the window that predates the uptime field, kept apart rather than assumed.
 *  A bucket with no searches carries only `at` and `n: 0` — the span is dense, so
 *  a quiet stretch draws as a gap rather than closing up. */
export interface ServedBucket {
  at: string
  n: number
  warm?: LatencyBand
  cold?: LatencyBand
  unknown?: LatencyBand
}

export interface Served {
  hours: number
  bucket: Bucket
  n: number
  n_unknown_regime: number
  buckets: ServedBucket[]
  warm: LatencyBand
  cold: LatencyBand
}

export interface StageRow {
  stage: string
  n: number
  p50: number
  p90: number
}

export interface Stages {
  n: number
  /** Rows whose process age is unknown — included, but not provably warm. */
  n_unproven: number
  stages: StageRow[]
}

/** Sparse, unlike `Served.buckets`: this is read as a table, and an empty row is
 *  noise where an empty chart point is information. */
export interface Restarts {
  n: number
  bucket: Bucket
  buckets: { at: string; n: number }[]
  p50_ms: number
  total_s: number
}

export interface BenchPoint {
  at: string
  commit: string | null
  p50: number
  p95: number
  p99: number
  n_queries: number
  tuning: boolean
}

export interface RetrievalReport {
  home: string
  hours: number
  bucket: Bucket
  at: string
  served: Served | null
  stages: Stages | null
  restarts: Restarts | null
  /** Keyed by query set. Two query sets are two populations of query and are
   *  never drawn as one line. */
  bench: Record<string, BenchPoint[]> | null
}

// --- the search lab's inventory --------------------------------------------
// What the bench has to measure with, as opposed to what it measured. Every row
// is read off the lab's own registries, so this describes the box rather than a
// catalog somebody kept up to date.

/** A walked subtree. `truncated` means the walk hit its file budget, so `bytes`
 *  is a floor rather than the size — render it as such, never as the total. */
export interface DirSize {
  bytes?: number
  files?: number
  truncated?: boolean
}

/** A built corpus home. `built: false` is a real row — an unbuilt corpus is what
 *  "available, not installed" looks like, and dropping it would make it
 *  indistinguishable from a corpus nobody defined. */
export interface CorpusHome extends DirSize {
  label: string
  path: string
  built: boolean
  snapshot_id: string | null
  counts: { events?: number; threads?: number; vectors?: number; kg_events?: number }
  embedding_space: string | null
  created_at: string | null
  /** Doc count off the harness's build marker, for homes that are built but never
   *  stamped as a snapshot (the haystack corpora). */
  build?: { docs: number | null; embedded: boolean }
  /** Set on a root holding many small homes (one per question). */
  homes?: number | null
}

export interface DatasetDownload extends DirSize {
  path: string | null
  present: boolean
}

export interface Dataset {
  name: string
  family: string
  harness: string
  download: DatasetDownload
  homes: CorpusHome[]
  /** Published baselines the harness already carries — the scale a measured
   *  number is read against. Shape differs by family. */
  reference: Record<string, unknown>
  /** Benchmark rows that run on this dataset. */
  on_bench: string[]
  source?: string
  license?: string
}

export interface BenchRun {
  at: string | null
  elapsed_s: number | null
  commit: string | null
  code_id: string | null
  measures: Record<string, number | null>
}

/** `missing` (no corpus on this box) outranks the rest: the row cannot run at
 *  all, so calling it stale would suggest a re-run is what it needs. `fresh` is
 *  the bench's own skip test — the ledger's numbers still describe what a run
 *  right now would measure. */
export type BenchState = 'missing' | 'fresh' | 'stale' | 'never-run'

export interface Benchmark {
  name: string
  argv: string[]
  corpus_home: string | null
  corpus_id: string | null
  corpus_built: boolean
  build_hint: string
  cost_min: number
  est_min: number
  fresh: boolean
  state: BenchState
  measure_keys: string[]
  code_id: string
  last: BenchRun | null
}

/** Nearest-rank percentiles, in milliseconds. Nearest-rank rather than
 *  interpolated: at a few hundred queries the p99 is one sample either way, and
 *  interpolating invents a latency no search actually took. */
export interface Percentiles {
  p50: number
  p95: number
  p99: number
}

/** What a run **cost**, as against what it scored.
 *
 *  Both come off the same searches, and only together say whether a
 *  configuration that scores better is one worth shipping — a pass that lifts
 *  nDCG and doubles p99 is a trade, not a win. Read as a lead rather than a
 *  benchmark: one sample per query under whatever conditions the run had. */
export interface RunPerformance {
  /** Searches timed. */
  queries?: number | null
  /** Wall-clock of the scored loop — not the whole process. The difference
   *  against the run's `elapsed_s` is setup: ingest, embed, model load. */
  scoring_s?: number | null
  qps?: number | null
  mean_ms?: number | null
  max_ms?: number | null
  total?: Percentiles
  /** Per-stage latency. The two pool arms run concurrently, so `fts_ms` and
   *  `semantic_ms` cover overlapping wall-clock and can sum past the total; only
   *  the shape stages sum. */
  stages?: Record<string, Percentiles>
  /** Searches that carried a stage breakdown. Below `queries` when a pool-cache
   *  hit sat the arms out — those did no retrieval rather than doing it fast. */
  staged?: number | null
  /** Searches that paid a model load inside them — the cold-model tail. */
  cold?: number | null
  pool_p50?: number | null
  corpus_docs?: number | null
  arms?: string[] | null
}

/** One scored query, as the run recorded it.
 *
 *  The aggregate says the row scored 0.494; this says which queries it failed.
 *  `rank` is the 1-based position of the first gold document, or null when none
 *  came back at all — "ranked 40th" is a ranking problem and "never retrieved"
 *  is a recall one, and a score of 0.0 reports them identically. */
export interface QueryRow {
  qid: string
  query: string
  latency_ms: number | null
  rank: number | null
  n_gold: number | null
  found: number | null
  measures: Record<string, number | null>
  /** The stratum the harness knows this query by — a category, a difficulty
   *  tier — so a failure reads as belonging to a kind. */
  group?: string
  /** Present only in a comparison: the same query at the other run. */
  before?: { measures: Record<string, number | null>; rank: number | null; latency_ms: number | null }
  /** Movement in the leading measure against the compared run. */
  moved?: number
}

export interface RunQueries {
  run_id: string
  /** The run this was joined against, when the rows carry `before`/`moved`. */
  compared_to: string | null
  order: 'worst' | 'moved'
  /** The metric these rows are read on — the first the harness listed. */
  lead: string | null
  total: number
  /** Queries whose gold document never came back at all. */
  misses: number
  returned: number
  rows: QueryRow[]
}

/** One row of the benchmark ledger — a run that happened, rather than the state
 *  a row is in. Everything the run recorded, plus the three facts establishing it
 *  takes the present: `id`, `on_bench`, `code_current`. */
export interface BenchRunRecord {
  /** Content hash of the record — a link's handle on it. The ledger is
   *  append-only, so it survives the file growing underneath. */
  id: string
  at: string
  row: string
  status: 'ok' | 'failed' | 'skipped'
  code_id: string | null
  corpus_id: string | null
  commit: string | null
  elapsed_s: number | null
  measures: Record<string, number | null>
  argv: string[]
  /** What the run cost. Absent on runs recorded before the harnesses reported
   *  one, and on failures — which is a different thing from a run that was fast. */
  performance?: RunPerformance | null
  /** Whether this run's per-query detail is still on disk. The store is capped,
   *  so an old run keeps its numbers and loses its detail. */
  has_queries?: boolean
  /** Historical: rows the ledger carries that the manifest no longer names. */
  tier?: string | null
  /** Whether this run's row is still a row the bench would run. */
  on_bench: boolean
  /** The newest successful run of its row — what the summary above reports. */
  current: boolean
  /** Whether it was measured under the ranking code now in the working tree.
   *  null for a row off the bench: there is no current code id for a harness the
   *  manifest no longer names, and false would invent one. */
  code_current: boolean | null
  measure_keys: string[]
}

export interface BenchRuns {
  code_id: string
  /** Records in the ledger, which `runs` may have been truncated from. */
  total: number
  returned: number
  runs: BenchRunRecord[]
}

export interface LabInventory {
  cache_root: string
  cache: DirSize
  /** The content hash of the ranking and scoring source as it sits in the working
   *  tree. A row measured under a different one is stale by definition. */
  code_id: string
  families: Record<string, string>
  benchmarks: Benchmark[]
  datasets: Dataset[]
}

// ── the drop zone (account-export upload) ───────────────────────────────────
// One bundle in `<home>/dumps/`. `bytes` is null for a directory (an export
// unpacked by hand) — the server doesn't walk a tree to size it.
export interface DropEntry {
  name: string
  bytes: number | null
  at: string | null
  // `imported` entries only: the retention slug of the provider that claimed it.
  kind?: string
}

// What the drop zone holds. `waiting` is yet to be imported — the watcher takes
// a bundle a poll or two after it lands, then it reappears under `imported` (kept
// as the recovery copy) or `failed` (quarantined, never deleted).
export interface DropZone {
  dumps_dir: string
  waiting: DropEntry[]
  imported: DropEntry[]
  failed: DropEntry[]
}

// An upload the server took: the name it landed under, and which provider's
// export it recognized it as.
export interface UploadAccepted {
  name: string
  kind: string
  label: string
  bytes: number
  dumps_dir: string
}

export interface ThreadListItem {
  id: string
  title: string | null
  source: string | null
  first_user_message: string | null
  // 'conversation' | 'system' (subagent runs) | 'topic' | legacy type strings —
  // an open vocabulary; /api/thread-types is the live census.
  thread_type: string
  updated_at: string | null
}

export interface ThreadTypeCount {
  thread_type: string
  threads: number
}

export interface ThreadPage {
  threads: ThreadListItem[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface SearchHit {
  event_id: number
  thread_id: string
  thread_title: string | null
  content_type: string | null
  snippet: string
  full_content: string
  occurred_at: string | null
  _semantic?: number
  // Ranked search only: how many of the query's terms literally appear in this
  // hit (the response's quality.n_terms is the denominator).
  term_hits?: number
  // Browse rows only (empty-query search: one row per thread, by last activity;
  // event_id is the thread's newest event — a ready tail anchor).
  thread_source?: string | null
  n_events?: number
}

// The top-hit match-quality verdict (the MCP header's signal): how much to
// trust the ranking before reading. Verdicts below strong carry a caution note.
export interface SearchQuality {
  verdict: 'strong' | 'partial' | 'weak' | 'semantic'
  note: string | null
  n_terms: number
}

// A curated subject the result set clusters under (the topic-graph lens);
// chats = how many of the result conversations it links.
export interface SearchSubject {
  topic_id: string
  title: string
  chats: number
}

export interface SearchResponse {
  query: string
  browse?: boolean
  hits: SearchHit[]
  quality?: SearchQuality | null
  subjects?: SearchSubject[]
  // ── where this page sits in the match set ────────────────────────────────
  // `total` counts the *set*, not the page — messages for a ranked search, threads
  // for a browse (`total_threads` carries the conversation count either way).
  total?: number | null
  total_threads?: number | null
  /** The totals are floors: the set scan stopped at its cap. Render them as `N+`. */
  capped?: boolean
  /** Every match is reachable by paging. False for a ranked search whose candidate
   *  pool saturated — the total is real, the walk is what stops at the pool, so
   *  `pages` counts what paging reaches rather than what exists. */
  exhaustive?: boolean
  page?: number
  pages?: number | null
  page_size?: number
}

// Binary content on a block (a pasted screenshot, a tool-result image, a
// document). `url` serves the bytes from the archive's blob store
// (/api/blob/<hash><ext>); a pointer-only ref (bytes never in the archive)
// has url: null and carries the provider pointer instead.
export interface BlockImage {
  kind: string // 'image' | 'pdf' | 'file'
  media_type: string | null
  bytes: number | null
  url: string | null
  pointer?: string | null
}

// One choice the assistant offered in an AskUserQuestion call. `preview` (a
// mockup, a diff, a code sketch) rides the call only — the recorded answer keeps
// labels and descriptions.
export interface AskOption {
  label: string
  description?: string
  preview?: string
}

export interface AskQuestion {
  question: string
  header?: string
  multiSelect?: boolean
  options: AskOption[]
}

export type Block =
  | { type: 'text'; text: string; images?: BlockImage[] }
  | { type: 'thinking'; text: string }
  | { type: 'tool_use'; name: string; input: unknown }
  // `answers`/`questions` appear on an AskUserQuestion result: the user's pick per
  // question (by option label, or their own text when they wrote one) alongside the
  // questions as recorded. The reader renders the pair as a decision, not as JSON.
  | {
      type: 'tool_result'
      output: string
      truncated: boolean
      images?: BlockImage[]
      answers?: Record<string, string>
      questions?: AskQuestion[]
    }
  // `denial_kind` ('user-rejected', …) marks a tool that never ran because the
  // user turned it down, as opposed to one that ran and failed.
  | { type: 'tool_error'; error: string; images?: BlockImage[]; denial_kind?: string }
  | { type: 'context_summary'; text: string }
  // A hook that fired and injected content into the model's context (a
  // hook_additional_context attachment or a hook-context sidecar line):
  // hook_name plus exactly what it injected.
  | { type: 'hook'; hook_name: string; text: string }
  // A bare "this hook ran" marker (hook_progress) — no content; consecutive
  // markers render merged into one compact row.
  | { type: 'hook_fired'; hook_name: string }
  // A preserved context injection (todo reminder, skill/tool listing delta, …)
  // with its real content, not just a placeholder label.
  | { type: 'attachment'; attachment_type: string; text: string }
  | { type: 'ide_context'; context_type: string; file_path?: string | null; text: string }
  | { type: 'content_block'; block_type: string; text: string }
  // A model switch marker. kind 'user' = a manual /model switch (a standalone divider
  // between turns, to_model only); kind 'fallback' = the active model's safeguards
  // flagged the message and Claude Code retried on a stronger one (from→to, mid-turn).
  // `safeguard_notice` is the human-readable reason paired with a fallback.
  | { type: 'model_switch'; kind: 'user' | 'fallback'; from_model: string | null; to_model: string | null }
  | { type: 'safeguard_notice'; text: string }
  | { type: 'unknown'; event_type: string; text: string }

// Per-message metadata for the info drawer. Every message carries `ts`; assistant
// messages also carry the model(s)/tokens/stop-reason folded from that turn's
// api_request events (different turns can be answered by different models).
export interface MessageMeta {
  ts: string | null
  models?: string[]
  requests?: number
  stop_reason?: string | null
  tokens?: { input: number; output: number; thinking: number }
}

export interface Message {
  // Usually user/assistant, but a preserved non-standard-role turn carries its
  // own role string (e.g. 'tool', 'developer'), so this isn't a closed set.
  role: string
  blocks: Block[]
  // Source events of this message's blocks — resolves a search hit's event id
  // to its message for deep-link + highlight.
  event_ids?: number[]
  meta?: MessageMeta
}

// The Task-tool subagent runs a thread spawned, tallied by model — null when it
// spawned none. `count` is the number of agent runs; `by_model` counts each run
// under its primary model, most-used first (its counts sum to `count`, minus any
// run whose model wasn't recorded).
export interface AgentSessions {
  count: number
  by_model: { model: string; count: number }[]
}

export interface StructuredThread {
  thread_id: string
  title: string | null
  source: string | null
  // Provenance for the reader header. `event_count` is the whole event log's
  // size (machinery included), so it normally exceeds messages.length.
  source_id?: string | null
  started_at?: string | null
  ended_at?: string | null
  event_count?: number
  agent_sessions?: AgentSessions | null
  messages: Message[]
}

export interface SourceCount {
  source: string
  threads: number
}

// The filters the search endpoint accepts beyond the query itself. since/until
// are ISO dates; source is one of /api/sources' names.
export interface SearchFilters {
  source?: string
  since?: string
  until?: string
}

// Rows per page of search results (ranked hits, or browse rows). The set is
// walked with ?page=, so this bounds one screen rather than the answer.
export const SEARCH_PAGE_SIZE = 40

// ── stats page ──────────────────────────────────────────────────────────────
export interface StatsOverview {
  conversations: number
  sources: number
  models: number
  input_tokens: number
  cache_read_tokens: number
  output_tokens: number
  tokens: number
  cost: number
  cost_conversations: number
  first_at: string | null
  last_at: string | null
}

export interface StatsSource {
  source: string
  conversations: number
  with_tokens: number
  input_tokens: number
  cache_read_tokens: number
  output_tokens: number
  tokens: number
  avg_tokens: number | null
  with_cost: number
  // null when this source records no cost at all (a subscription tool); a number
  // (possibly 0) when at least one of its sessions carried a cost.
  cost: number | null
  avg_cost: number | null
}

export interface StatsModel {
  model: string
  requests: number
  input_tokens: number
  cache_read_tokens: number
  output_tokens: number
  tokens: number
  cost: number | null
  conversations: number
}

// One stacked/faceted band of a monthly chart: `key` is a source or model name (or
// 'other', the folded tail), `values` runs parallel to `timeline.months`.
export interface StatsSeries {
  key: string
  values: number[]
}

// The month axis is dense — every calendar month between the first and the last, quiet
// ones included. Conversations bucket by the month a session *started*; tokens by the
// month each *request* happened, so a session spanning a boundary spends in both.
export interface StatsTimeline {
  months: string[] // 'YYYY-MM'
  // Conversations with no events at all, and so no date — excluded from the series
  // above, reported so the chart's total can be seen not to match the overview tile.
  undated: number
  conversations: number[]
  tokens: number[]
  cost: Array<number | null>
  conversations_by_source: StatsSeries[]
  tokens_by_model: StatsSeries[]
}

// Sessions that recorded no token usage at all are not binned as zero — they are
// counted in `without_tokens` instead, so the histogram doesn't grow a spike of tiny
// sessions that never happened. `hi` is null on the open-ended top bucket.
export interface StatsSessionSizes {
  buckets: Array<{ lo: number; hi: number | null; count: number }>
  sessions: number
  without_tokens: number
  median: number | null
  p90: number | null
}

// When sessions start, weekday × hour in the server's local time; rows are Monday-first.
export interface StatsRhythm {
  grid: number[][]
  max: number
  total: number
}

export interface Stats {
  overview: StatsOverview
  by_source: StatsSource[]
  by_model: StatsModel[]
  timeline: StatsTimeline
  session_sizes: StatsSessionSizes
  rhythm: StatsRhythm
}

// ── per-model drill-down (/stats/model/:model) ──────────────────────────────
// Sessions are the conversations in which the model answered at least one request;
// token/request/cost figures are the model's own share of each. Compactions are
// counted across those sessions (the event doesn't say which model's context
// overflowed, so in a mixed-model session they read as "compactions in sessions
// this model took part in").
export interface ModelStatsOverview {
  conversations: number
  requests: number
  input_tokens: number
  cache_read_tokens: number
  output_tokens: number
  thinking_tokens: number
  tokens: number
  cost: number | null
  cost_conversations: number
  compactions: number
  first_at: string | null
  last_at: string | null
}

export interface ModelStatsPerSession {
  min_tokens: number
  max_tokens: number
  avg_tokens: number | null
  median_tokens: number | null
  avg_requests: number | null
}

export interface ModelStatsMonth {
  month: string // 'YYYY-MM'
  sessions: number
  requests: number
  input_tokens: number
  cache_read_tokens: number
  output_tokens: number
  tokens: number
  avg_tokens: number | null
  cost: number | null
  compactions: number
}

export interface ModelStatsSession {
  thread_id: string
  title: string | null
  source: string
  at: string | null
  tokens: number
  cache_read_tokens: number
  requests: number
  compactions: number
}

export interface ModelStats {
  model: string
  overview: ModelStatsOverview
  per_session: ModelStatsPerSession
  by_month: ModelStatsMonth[]
  top_sessions: ModelStatsSession[]
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json() as Promise<T>
}

/**
 * POST with no body, answering JSON — the shape every write here but the export
 * upload takes (the subject rides the query string).
 *
 * `X-Archive-Write` is the server's cross-site guard, not decoration: no HTML
 * form can set a custom header, so sending one forces a preflight the server
 * never answers, which is what keeps another page's form from writing at this
 * port. Failures come back as the server's plain-text reason.
 */
async function postJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: 'POST', headers: { 'X-Archive-Write': '1' } })
  if (!r.ok) throw new Error((await r.text()).trim() || `request failed (${r.status})`)
  return r.json() as Promise<T>
}

/**
 * POST one account-export ZIP to the drop zone, reporting upload progress.
 *
 * XHR rather than fetch: an account export is routinely gigabytes, and fetch
 * exposes no upload progress at all — a multi-minute send with no bar is
 * indistinguishable from a hung one.
 *
 * `X-Archive-Write` is the server's cross-site guard, not decoration. No HTML
 * form can set a custom header, so sending one forces a preflight that the
 * server never answers — which is what keeps some other page's form from
 * posting at this port. Rejections come back as a plain-text reason to show.
 */
export function uploadExport(
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<UploadAccepted> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', '/api/upload?name=' + encodeURIComponent(file.name))
    request.setRequestHeader('X-Archive-Write', '1')
    request.setRequestHeader('Content-Type', 'application/zip')
    if (onProgress) {
      request.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total)
      })
    }
    request.addEventListener('load', () => {
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(request.responseText.trim() || `upload failed (${request.status})`))
        return
      }
      try {
        resolve(JSON.parse(request.responseText) as UploadAccepted)
      } catch {
        reject(new Error('the server accepted the upload but answered unreadably'))
      }
    })
    request.addEventListener('error', () => reject(new Error('the connection to the archive failed')))
    request.addEventListener('abort', () => reject(new Error('upload cancelled')))
    request.send(file)
  })
}

export const api = {
  status: () => getJSON<Status>('/api/status'),
  // The action queue, silences already applied. Separate from status(): it costs
  // no index counting, and silencing one has to re-read the queue right away.
  notices: () => getJSON<NoticeBoard>('/api/notices'),
  // Both writes answer with the whole board, so the page never has to guess what
  // the store now holds — it renders what the server just committed.
  silenceNotice: (key: string) =>
    postJSON<NoticeBoard>('/api/notices/silence?key=' + encodeURIComponent(key)),
  unsilenceNotice: (key: string) =>
    postJSON<NoticeBoard>('/api/notices/unsilence?key=' + encodeURIComponent(key)),
  // Live load progress and the run history. Cheap by construction (two small
  // files off the home, no index counting), so a page watching a running load
  // can poll it without competing with the load for the store.
  loads: (limit = 20) => getJSON<LoadStatus>(`/api/loads?limit=${limit}`),
  // Separate from status(): this one walks the home, so it is fetched on its own
  // (slower) cadence rather than riding the health page's 30s status poll.
  disk: () => getJSON<DiskUsage>('/api/disk'),
  // No `types` → the server's default view (topics and system/subagent runs
  // hidden); an explicit list selects exactly those thread types.
  threadPage: (
    opts: { q?: string; types?: string[]; limit?: number; page?: number } = {},
  ) => {
    const params = new URLSearchParams({
      limit: String(opts.limit ?? 150),
      page: String(opts.page ?? 1),
    })
    if (opts.q) params.set('q', opts.q)
    if (opts.types) params.set('types', opts.types.join(','))
    return getJSON<ThreadPage>('/api/threads?' + params.toString())
  },
  threads: (opts: { q?: string; types?: string[]; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(opts.limit ?? 150), page: '1' })
    if (opts.q) params.set('q', opts.q)
    if (opts.types) params.set('types', opts.types.join(','))
    return getJSON<ThreadPage>('/api/threads?' + params.toString()).then((d) => d.threads)
  },
  threadTypes: () =>
    getJSON<{ types: ThreadTypeCount[] }>('/api/thread-types').then((d) => d.types),
  // An empty q browses: one row per thread by last activity, same filters. Both
  // shapes page (1-based); the response says where the page sits in the set.
  search: (q: string, filters: SearchFilters = {}, page = 1) => {
    const params = new URLSearchParams({
      limit: String(SEARCH_PAGE_SIZE),
      q,
      page: String(page),
    })
    for (const key of ['source', 'since', 'until'] as const)
      if (filters[key]) params.set(key, filters[key])
    return getJSON<SearchResponse>('/api/search?' + params.toString())
  },
  sources: () =>
    getJSON<{ sources: SourceCount[] }>('/api/sources').then((d) => d.sources),
  thread: (id: string, opts: { thinking: boolean; tools: boolean }) =>
    getJSON<StructuredThread>(
      `/api/thread/${id}?thinking=${opts.thinking ? 1 : 0}&tools=${opts.tools ? 1 : 0}`,
    ),
  // Resolve a pasted thread ref (a claude-code/codex session uuid or stem, or a
  // legacy integer id) to its ULID archive thread. No source param → the server
  // searches every provider.
  resolveLink: (id: string) =>
    getJSON<{ thread_id: string; url: string }>(
      '/api/archive-link?id=' + encodeURIComponent(id),
    ),
  // What is in the drop zone right now — the upload page's progress signal,
  // since the import itself is the watcher's work, not the server's.
  drops: () => getJSON<DropZone>('/api/drops'),
  stats: () => getJSON<Stats>('/api/stats'),
  // Model ids can contain '/' (router models), so the name is a percent-encoded
  // path tail, not a query param — the server decodes it back.
  modelStats: (model: string) =>
    getJSON<ModelStats>('/api/stats/model/' + encodeURIComponent(model)),
  // How search itself is doing — read off the retrieval ledgers, not the index,
  // so it keeps answering while a rebuild has the corpus unavailable. The window
  // is hours because the useful ones are short: a regression that lands at noon
  // is invisible in a 14-day median for a week. A dev page: the report is the
  // search lab's, so an install without the lab answers 404 and the view says so.
  retrieval: (hours = 14 * 24) => getJSON<RetrievalReport>(`/api/retrieval?hours=${hours}`),
  // What the bench has on hand: benchmark rows and whether each can run, and the
  // corpora on disk and what they hold. A dev page like `retrieval` and for the
  // same reason — the inventory is the search lab's, and an install has no lab to
  // inventory, so it answers 404.
  searchLab: () => getJSON<LabInventory>('/api/search-lab'),
  // Every run the bench ever recorded here, newest first — the ledger, not the
  // newest-per-row summary `searchLab` carries. Its own call because the
  // inventory is a filesystem walk served from a cache, and a run that finished
  // a second ago has to show up in the history now.
  searchLabRuns: (row?: string) =>
    getJSON<BenchRuns>('/api/search-lab/runs' + (row ? `?row=${encodeURIComponent(row)}` : '')),
  // One run's per-query detail, worst first — or, with `vs`, the queries that
  // moved against another run, biggest regression first. Off the run's own
  // sidecar, so this costs one file open and the runs list costs none.
  searchLabRunQueries: (id: string, vs?: string) =>
    getJSON<RunQueries>(
      `/api/search-lab/runs/${encodeURIComponent(id)}/queries` +
        (vs ? `?vs=${encodeURIComponent(vs)}` : ''),
    ),
}
