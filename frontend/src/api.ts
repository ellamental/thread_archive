// The viewer's data layer: typed fetches against the four JSON endpoints the
// stdlib `archive web` server exposes. No client framework state — plain fetch.

export interface Status {
  threads: number
  events: number
  topics: number
  fts_indexed: number
  vectors_indexed: number
  home: string
}

export interface ThreadListItem {
  id: number
  title: string | null
  source: string | null
  updated_at: string | null
}

export interface SearchHit {
  event_id: number
  thread_id: number
  thread_title: string | null
  content_type: string | null
  snippet: string
  full_content: string
  occurred_at: string | null
  _semantic?: number
}

export interface SearchResponse {
  query: string
  hits: SearchHit[]
}

export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'tool_use'; name: string; input: unknown }
  | { type: 'tool_result'; output: string; truncated: boolean }
  | { type: 'tool_error'; error: string }
  | { type: 'context_summary'; text: string }
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
  meta?: MessageMeta
}

export interface StructuredThread {
  thread_id: number
  title: string | null
  source: string | null
  messages: Message[]
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`)
  return r.json() as Promise<T>
}

export const api = {
  status: () => getJSON<Status>('/api/status'),
  threads: (q?: string) =>
    getJSON<{ threads: ThreadListItem[] }>(
      '/api/threads?limit=150' + (q ? '&q=' + encodeURIComponent(q) : ''),
    ).then((d) => d.threads),
  search: (q: string) =>
    getJSON<SearchResponse>('/api/search?limit=40&q=' + encodeURIComponent(q)),
  thread: (id: number, opts: { thinking: boolean; tools: boolean }) =>
    getJSON<StructuredThread>(
      `/api/thread/${id}?thinking=${opts.thinking ? 1 : 0}&tools=${opts.tools ? 1 : 0}`,
    ),
  // Resolve a pasted provider session id (a cloth/claude-code/codex uuid or stem) to
  // its numeric archive thread. No source param → the server searches every provider.
  resolveLink: (id: string) =>
    getJSON<{ thread_id: number; url: string }>(
      '/api/archive-link?id=' + encodeURIComponent(id),
    ),
}
