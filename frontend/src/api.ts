// The viewer's data layer: typed fetches against the JSON endpoints the cohosted
// stdlib server (`archive watch --web`) exposes. No client framework state — plain fetch.

export interface Status {
  threads: number
  events: number
  topics: number
  fts_indexed: number
  vectors_indexed: number
  home: string
}

export interface ThreadListItem {
  id: string
  title: string | null
  source: string | null
  // 'conversation' | 'system' (subagent runs) | 'topic' | legacy type strings —
  // an open vocabulary; /api/thread-types is the live census.
  thread_type: string
  updated_at: string | null
}

export interface ThreadTypeCount {
  thread_type: string
  threads: number
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

export interface TopicListItem {
  id: string
  title: string | null
  topic_kind: string | null
  description: string | null
  evidence_count: number
  link_count: number
  community: number | null
  pagerank: number
  updated_at: string | null
}

export interface TopicGraphStatus {
  available: boolean
  nodes?: number
  communities?: number
  components?: number
  community_engine?: string
}

export interface TopicsResponse {
  topics: TopicListItem[]
  graph: TopicGraphStatus
}

export interface TopicTreeNode {
  id: string
  title: string | null
  topic_kind: string | null
  children: TopicTreeNode[]
}

export interface TopicTreeResponse {
  roots: TopicTreeNode[]
  topics_in_hierarchy: number
  topics_total: number
}

export interface TopicLink {
  direction: 'out' | 'in'
  other_id: string
  other_title: string | null
  other_type: string // 'topic' | 'conversation'
  link_type: string
  strength: number
  evidence: string | null
}

export interface TopicEvidence {
  event_id: number
  thread_id: string
  thread_title: string | null
  quote: string
  created_at: string | null
}

export interface TopicPeer {
  thread_id: string
  title: string | null
  pagerank: number
}

export interface TopicDetail {
  id: string
  title: string | null
  topic_kind: string | null
  description: string | null
  summary: string | null
  archived: boolean
  created_at: string | null
  updated_at: string | null
  // null when the graph has no live node for this topic (archived, or unlinked
  // before the projection saw it)
  graph: { pagerank: number; community: number | null; degree: number } | null
  links: TopicLink[]
  evidence: TopicEvidence[]
  peers: TopicPeer[]
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

// ── curation: what the librarian and gardener drains have done ──────────────

export interface CurationDrain {
  // null = the backlog gate query failed. The daemon fails open and launches
  // anyway, so this must not render as "drained".
  backlog: number | null
  batch: number
  model: string
  effort: string | null
  cadence: { kind: 'interval'; interval_s: number } | { kind: 'daily'; at: string }
  // When the drain last fired — launched or skipped. null = never fired here.
  heartbeat_at: string | null
  heartbeat_age_s: number | null
  // Librarian only: what it curates. A horizon means conversations from that
  // point on; everything older is `history`, reached at `catchup_per_run` a run
  // (0 = never). No horizon means the whole archive is fair game.
  policy?: {
    horizon: string | null
    catchup_per_run: number
    forward: number | null
    history: number | null
  }
}

export interface CurationGraph {
  topics: number
  in_hierarchy: number
  singletons: number
  uncited: number
  unparented: number
  dupe_pairs: number
  hierarchy_pct: number | null
}

export interface CurationCoverage {
  conversations: number
  summarized: number
  cited: number
  topics_live: number
  topics_archived: number
  citations: number
  links: number
}

// Conversation threads carrying events but no message — an ingest condition, not
// backlog: no drain can ever clear them, so they're counted here instead of
// silently sitting outside every queue.
export interface CurationUncuratable {
  threads: number
  sample: { id: string; title: string | null; source: string | null; event_types: string | null }[]
}

export interface CurationDay {
  day: string
  citations: number
  links: number
  topics: number
}

export interface CurationRunDay {
  day: string
  librarian: number
  gardener: number
  requests: number
  output_tokens: number
}

export interface CurationRun {
  id: string
  kind: 'librarian' | 'gardener' | 'unknown'
  title: string | null
  started_at: string | null
  requests: number
  output_tokens: number
  model: string | null
}

export interface Curation {
  generated_at: string
  days: number
  drains: { librarian: CurationDrain; gardener: CurationDrain }
  graph: CurationGraph
  coverage: CurationCoverage
  uncuratable: CurationUncuratable
  activity: CurationDay[]
  runs: { by_day: CurationRunDay[]; recent: CurationRun[] }
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json() as Promise<T>
}

export const api = {
  status: () => getJSON<Status>('/api/status'),
  // No `types` → the server's default view (topics and system/subagent runs
  // hidden); an explicit list selects exactly those thread types.
  threads: (opts: { q?: string; types?: string[]; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(opts.limit ?? 150) })
    if (opts.q) params.set('q', opts.q)
    if (opts.types) params.set('types', opts.types.join(','))
    return getJSON<{ threads: ThreadListItem[] }>('/api/threads?' + params.toString()).then(
      (d) => d.threads,
    )
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
  topics: () => getJSON<TopicsResponse>('/api/topics'),
  topicTree: () => getJSON<TopicTreeResponse>('/api/topics/tree'),
  topic: (id: string) => getJSON<TopicDetail>(`/api/topic/${id}`),
  stats: () => getJSON<Stats>('/api/stats'),
  curation: () => getJSON<Curation>('/api/curation'),
  // Model ids can contain '/' (router models), so the name is a percent-encoded
  // path tail, not a query param — the server decodes it back.
  modelStats: (model: string) =>
    getJSON<ModelStats>('/api/stats/model/' + encodeURIComponent(model)),
}
