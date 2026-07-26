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
  tag?: string
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

// A load of one archive — live (status 'running') or finished. `stalled` means
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

export interface ArchiveEntry {
  id: string
  home: string
  label: string
  first_seen?: string
  last_opened?: string
  // Descriptive tag set by the operator (or by tooling — a snapshot stamps
  // 'snapshot' on its dest): what this archive is *for*. Grants nothing.
  role?: string
  exists: boolean
  active: boolean
  index_bytes?: number
  // The live load state published by whatever process is loading this archive —
  // {} when no load has ever been recorded for it.
  load: LoadRun | Record<string, never>
  runs?: LoadRun[]
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
  // Other threads whose matching text is identical to this hit's — a forked
  // session, a fleet of agents carrying one prompt. Folded into this row by the
  // search rather than repeated as rows of their own.
  dup_threads?: DupThread[]
  // Browse rows only (empty-query search: one row per thread, by last activity;
  // event_id is the thread's newest event — a ready tail anchor).
  thread_source?: string | null
  n_events?: number
}

export interface DupThread {
  thread_id: string
  title: string | null
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

export type Block =
  | { type: 'text'; text: string; images?: BlockImage[] }
  | { type: 'thinking'; text: string }
  | { type: 'tool_use'; name: string; input: unknown }
  | { type: 'tool_result'; output: string; truncated: boolean; images?: BlockImage[] }
  | { type: 'tool_error'; error: string; images?: BlockImage[] }
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

// One page of hits; when a response comes back full the UI says "top N" and
// asks for a narrower query instead of pretending the list is complete.
export const SEARCH_LIMIT = 40

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

export interface Stats {
  overview: StatsOverview
  by_source: StatsSource[]
  by_model: StatsModel[]
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

export interface QualityPoint {
  at: string
  commit: string | null
  passed: boolean
  mrr: number
  ndcg: number
  n: number
}

export interface RetrievalReport {
  home: string
  hours: number
  bucket: Bucket
  at: string
  served: Served | null
  stages: Stages | null
  restarts: Restarts | null
  /** Keyed by query set — `gold` and `observed` are different populations of
   *  query and are never drawn as one line. */
  bench: Record<string, BenchPoint[]> | null
  quality: { points: QualityPoint[]; latest: QualityPoint | null } | null
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json() as Promise<T>
}

/**
 * POST one account-export ZIP to the drop zone, reporting upload progress.
 *
 * XHR rather than fetch: an account export is routinely gigabytes, and fetch
 * exposes no upload progress at all — a multi-minute send with no bar is
 * indistinguishable from a hung one.
 *
 * `X-Archive-Upload` is the server's cross-site guard, not decoration. No HTML
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
    request.setRequestHeader('X-Archive-Upload', '1')
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
  archives: () =>
    getJSON<{ archives: ArchiveEntry[] }>('/api/archives').then((d) => d.archives),
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
  // An empty q browses: one row per thread by last activity, same filters.
  search: (q: string, filters: SearchFilters = {}) => {
    const params = new URLSearchParams({ limit: String(SEARCH_LIMIT), q })
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
  // is invisible in a 14-day median for a week.
  retrieval: (hours = 14 * 24) => getJSON<RetrievalReport>(`/api/retrieval?hours=${hours}`),
}
