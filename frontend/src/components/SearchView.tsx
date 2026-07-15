import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, type SearchHit } from '../api'

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
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const q = params.get('q') ?? ''
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!q) return
    setHits(null)
    setErr(null)
    api
      .search(q)
      .then((r) => setHits(r.hits))
      .catch((e) => setErr(String(e.message ?? e)))
  }, [q])

  if (!q) return <div className="wrap"><div className="empty">Type a query above.</div></div>

  return (
    <div className="wrap">
      <div className="submeta">results for “{q}”</div>
      {err && <div className="empty">search error: {err}</div>}
      {!err && hits === null && <div className="empty">searching…</div>}
      {!err && hits && hits.length === 0 && <div className="empty">no matches</div>}
      {hits &&
        group(hits).map((g) => (
          <div className="group" key={g.threadId}>
            <button className="gh" onClick={() => navigate('/archive/' + g.threadId)}>
              <span>{g.title || 'thread ' + g.threadId}</span>
              <span className="src">
                #{g.threadId} · {g.hits.length} hit{g.hits.length > 1 ? 's' : ''}
              </span>
            </button>
            {g.hits.map((h) => (
              <button
                className="hit"
                key={h.event_id}
                onClick={() => navigate(`/archive/${g.threadId}?e=${h.event_id}`)}
              >
                <div className="snip">{h.snippet || h.full_content.slice(0, 280)}</div>
                <div className="meta">
                  {h.content_type && <span className="badge">{h.content_type}</span>}
                  {h._semantic != null && <span className="badge sem">semantic</span>}
                  {h.occurred_at && <span className="badge">{fmtDate(h.occurred_at)}</span>}
                </div>
              </button>
            ))}
          </div>
        ))}
    </div>
  )
}
