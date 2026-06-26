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

export interface Message {
  role: 'user' | 'assistant'
  blocks: Block[]
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
}
