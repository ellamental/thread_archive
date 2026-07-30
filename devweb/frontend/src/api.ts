/**
 * The dev panels' API client — the three endpoints `devweb/server.py` serves,
 * and the types they answer with.
 *
 * A separate file from the viewer's `api.ts` rather than a shared import: these
 * are different servers now, and the only thing the two clients ever had in
 * common was `getJSON`. Duplicating four lines is cheaper than a package
 * boundary between two apps that are each one screen of routes.
 */

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json() as Promise<T>
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
 *  is the window that predates the stage probe, kept apart rather than assumed.
 *  A bucket with no searches carries only `at` and `n: 0` — the span is dense, so
 *  a quiet stretch draws as a gap rather than closing up. */
export interface ServedBucket {
  at: string
  n: number
  warm?: LatencyBand
  cold?: LatencyBand
  unknown?: LatencyBand
}

/** One front door's traffic, carrying every headline number the page quotes.
 *
 *  `mcp-http` is the shared always-on server, which warms at startup; `mcp-stdio`
 *  and `cli` are one process per call and pay that load inside their first (and
 *  only) search, so they are near-entirely cold by construction; `web` is this
 *  viewer, long-lived and warmed, whose searches are recorded in its own request
 *  ledger and read back alongside the tools'. `mcp` is unattributed — rows written
 *  before the surfaces declared themselves, not a door of its own.
 *
 *  `p50`/`p90` are the door's whole distribution, every regime and workload in it;
 *  the bands beside them are the cuts worth quoting. */
export interface SurfaceRow {
  surface: string
  n: number
  n_cold: number
  p50: number
  p90: number
  /** What asking this door a question costs: warm, first page, ordinary width. */
  warm_interactive: LatencyBand
  warm_bulk: LatencyBand
  cold: LatencyBand
}

export interface Served {
  hours: number
  bucket: Bucket
  n: number
  n_unknown_regime: number
  buckets: ServedBucket[]
  warm: LatencyBand
  /** Warm first-page searches with an ordinary limit — what asking a question
   *  costs. Kept apart from `warm_bulk` (pagination sweeps, wide exports) so the
   *  median measures the question, not the window's workload mix. These pool every
   *  door together, so they are a total rather than anyone's experience — quote
   *  them as `all doors`, never as the headline. */
  warm_interactive: LatencyBand
  warm_bulk: LatencyBand
  cold: LatencyBand
  by_surface: SurfaceRow[]
}

export interface StageRow {
  stage: string
  n: number
  p50: number
  p90: number
}

export interface Stages {
  n: number
  /** Rows the stage probe never touched — included, but not provably warm. */
  n_unproven: number
  stages: StageRow[]
}

/** Sparse, unlike `Served.buckets`: this is read as a table, and an empty row is
 *  noise where an empty chart point is information. */
export interface Restarts {
  n: number
  bucket: Bucket
  buckets: { at: string; n: number }[]
  /** Which daemon restarted, and what a start cost it. Several warm independently,
   *  so the total says how much warming the box did and only this says how often
   *  one service bounced. A door missing here never warms on purpose. */
  by_surface: { surface: string; n: number; p50_ms: number }[]
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
  /** Whether the retrieval ledger under these sections is still being written.
   *  It records only where `"dev_mode": true`, so a false here means the page is
   *  history rather than a quiet window. */
  recording: boolean
}

// --- developer telemetry ---------------------------------------------------
// These are summaries over retained operational ledgers, not corpus statistics.
// The page is mounted only when the served shell enables developer panels.

export interface TelemetryWebEndpoint {
  method: string
  path: string
  n: number
  errors: number
  bytes: number
  concurrent: number
  p50: number
  p95: number
  p99: number
  max: number
}

export interface TelemetryWeb {
  requests: number
  errors: number
  bytes: number
  concurrent: number
  p50: number
  p95: number
  p99: number
  max: number
  endpoints: TelemetryWebEndpoint[]
  retained_bytes: number
}

export interface TelemetryIngestSource {
  passes: number
  items: number
  events: number
  lines: number
  bytes: number
  errors: number
  pass_p50_ms: number
  pass_p95_ms: number
  total_s: number
}

export interface TelemetryBackgroundWork {
  passes: number
  total_s: number
  p95_ms: number
  embedded?: number
}

/** The poll loop's floor: passes that found nothing, rolled up per window. */
export interface TelemetryIdle {
  passes: number
  total_s: number
  max_ms: number
  per_pass_ms: number
}

export interface TelemetryIngest {
  hours: number
  sources: Record<string, TelemetryIngestSource>
  stages: Record<string, number>
  maintenance: TelemetryBackgroundWork
  embed: TelemetryBackgroundWork
  idle?: TelemetryIdle
  retained_bytes: number
}

export interface TelemetryFault {
  signature: string
  source: string
  count: number
  first: string
  last: string
  sample: string
}

export interface TelemetryLedger {
  file: string
  label: string
  view: 'telemetry' | 'retrieval' | 'health' | string
  bytes: number
  segments: number
  /** Whether this install is still writing this ledger. Runtime telemetry
   *  records only where `"dev_mode": true`; fault ledgers record everywhere. */
  recording: boolean
}

export interface TelemetryReport {
  home: string
  hours: number
  at: string
  web: TelemetryWeb
  ingest: TelemetryIngest
  faults: TelemetryFault[]
  ledgers: TelemetryLedger[]
  /** False when this install records no runtime telemetry at all — the one state
   *  where an empty page means "nothing is written down" rather than "nothing
   *  happened", and the fix is a config line rather than a longer window. */
  recording: boolean
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


/** The window every windowed view opens on.
 *
 *  One value, not one per view: the overview quotes retrieval and telemetry side
 *  by side and links to both, and a page that opened on a default of its own
 *  showed the same table with different numbers in it — which reads as the
 *  instruments disagreeing when it is one stretch of time against another. */
export const DEFAULT_HOURS = 14 * 24

export const api = {
  // How search itself is doing — read off the retrieval ledgers, not the index,
  // so it keeps answering while a rebuild has the index unavailable.
  retrieval: (hours = DEFAULT_HOURS) => getJSON<RetrievalReport>(`/api/retrieval?hours=${hours}`),

  // The operational ledgers a maintainer reads together.
  telemetry: (hours = DEFAULT_HOURS) => getJSON<TelemetryReport>(`/api/telemetry?hours=${hours}`),

  // What the bench has to measure with: which benchmark rows are runnable, which
  // corpora on disk and what they hold.
  searchLab: () => getJSON<LabInventory>('/api/search-lab'),

  // Every recorded run, not the newest-per-row summary `searchLab` carries. Its
  // own call because the inventory is cached behind a filesystem walk and a run
  // that just finished has to appear now.
  searchLabRuns: (row?: string) =>
    getJSON<BenchRuns>('/api/search-lab/runs' + (row ? `?row=${encodeURIComponent(row)}` : '')),

  // One run's per-query detail, optionally diffed against another run.
  searchLabRunQueries: (id: string, vs?: string) =>
    getJSON<RunQueries>(
      `/api/search-lab/runs/${encodeURIComponent(id)}/queries` +
        (vs ? `?vs=${encodeURIComponent(vs)}` : ''),
    ),
}
