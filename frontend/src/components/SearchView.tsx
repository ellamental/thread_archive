import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, SEARCH_LIMIT, type SearchHit } from '../api'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

interface Group {
  threadId: number
  title: string | null
  hits: SearchHit[]
}

function group(hits: SearchHit[]): Group[] {
  const out: Group[] = []
  const byId = new Map<number, Group>()
  for (const h of hits) {
    let g = byId.get(h.thread_id)
    if (!g) {
      g = { threadId: h.thread_id, title: h.thread_title, hits: [] }
      byId.set(h.thread_id, g)
      out.push(g)
    }
    g.hits.push(h)
  }
  return out
}

export function SearchView() {
  // Query and filters both live in the URL (the sidebar's search widget writes
  // them), so navigating into a thread and coming back — or sharing the
  // address — restores the exact same result page.
  const [params] = useSearchParams()
  const q = params.get('q') ?? ''
  const source = params.get('source') ?? ''
  const since = params.get('since') ?? ''
  const until = params.get('until') ?? ''
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setHits(null)
    setErr(null)
    if (!q) return
    // Stale guard: only the latest request may land — without it a slow older
    // search can resolve after a newer one and silently replace its results.
    let live = true
    // A bare `until` date resolves server-side to that day's midnight, which
    // excludes the day itself; a "to" filter should include the chosen day.
    api
      .search(q, {
        source: source || undefined,
        since: since || undefined,
        until: until ? until + 'T23:59:59' : undefined,
      })
      .then((r) => live && setHits(r.hits))
      .catch((e) => live && setErr(String(e.message ?? e)))
    return () => {
      live = false
    }
  }, [q, source, since, until])

  if (!q) return <div className="wrap"><div className="empty">Type a query above.</div></div>

  const filters = [source, since && `from ${since}`, until && `to ${until}`].filter(Boolean)

  return (
    <div className="wrap">
      <div className="submeta">
        results for “{q}”{filters.length > 0 && ` · ${filters.join(' · ')}`}
      </div>
      {err && <div className="empty">search error: {err}</div>}
      {!err && hits === null && <div className="empty">searching…</div>}
      {!err && hits && hits.length === 0 && <div className="empty">no matches</div>}
      {hits && hits.length >= SEARCH_LIMIT && (
        <div className="submeta">top {SEARCH_LIMIT} hits shown — narrow the query or filters to see the rest</div>
      )}
      {hits &&
        group(hits).map((g) => (
          <div className="group" key={g.threadId}>
            <Link className="gh" to={'/archive/' + g.threadId}>
              <span>{g.title || 'thread ' + g.threadId}</span>
              <span className="src">
                #{g.threadId} · {g.hits.length} hit{g.hits.length > 1 ? 's' : ''}
              </span>
            </Link>
            {g.hits.map((h) => (
              <Link className="hit" key={h.event_id} to={`/archive/${g.threadId}?e=${h.event_id}`}>
                <div className="snip">{h.snippet || h.full_content.slice(0, 280)}</div>
                <div className="meta">
                  {h.content_type && <span className="badge">{h.content_type}</span>}
                  {h._semantic != null && <span className="badge sem">semantic</span>}
                  {h.occurred_at && <span className="badge">{fmtDate(h.occurred_at)}</span>}
                </div>
              </Link>
            ))}
          </div>
        ))}
    </div>
  )
}
