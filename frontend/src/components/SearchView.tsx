import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, SEARCH_LIMIT, type SearchHit, type SearchResponse } from '../api'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

interface Group {
  threadId: string
  title: string | null
  hits: SearchHit[]
}

function group(hits: SearchHit[]): Group[] {
  const out: Group[] = []
  const byId = new Map<string, Group>()
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
  // address — restores the exact same result page. An empty query is a browse:
  // recent threads, one row each, honoring the same filters.
  const [params] = useSearchParams()
  const q = params.get('q') ?? ''
  const source = params.get('source') ?? ''
  const since = params.get('since') ?? ''
  const until = params.get('until') ?? ''
  const [resp, setResp] = useState<SearchResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // Which hits have their duplicate-thread fold expanded, by event id. The fold
  // hides threads from a browsable list, so it stays openable rather than being
  // a bare count of things the reader can't reach.
  const [openDups, setOpenDups] = useState<Set<number>>(new Set())
  const browse = !q

  useEffect(() => {
    setResp(null)
    setErr(null)
    setOpenDups(new Set())
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
      .then((r) => live && setResp(r))
      .catch((e) => live && setErr(String(e.message ?? e)))
    return () => {
      live = false
    }
  }, [q, source, since, until])

  const filters = [source, since && `from ${since}`, until && `to ${until}`].filter(Boolean)
  const hits = resp?.hits ?? null
  const quality = resp?.quality
  const subjects = resp?.subjects ?? []

  return (
    <div className="wrap">
      <div className="submeta">
        {browse ? 'recent threads — newest activity first' : `results for “${q}”`}
        {filters.length > 0 && ` · ${filters.join(' · ')}`}
        {quality && (
          <>
            {' · '}
            <span className={'badge quality-' + quality.verdict}>quality: {quality.verdict}</span>
          </>
        )}
      </div>
      {quality?.note && <div className="quality-note">{quality.note}</div>}
      {subjects.length > 0 && (
        <div className="subjects">
          <span className="subjects-label">subjects:</span>
          {subjects.map((s) => (
            <Link className="chip" key={s.topic_id} to={'/topic/' + s.topic_id}>
              {s.title} ({s.chats})
            </Link>
          ))}
        </div>
      )}
      {err && <div className="empty">search error: {err}</div>}
      {!err && hits === null && <div className="empty">{browse ? 'loading…' : 'searching…'}</div>}
      {!err && hits && hits.length === 0 && (
        <div className="empty">{browse ? 'no threads in this window' : 'no matches'}</div>
      )}
      {hits && hits.length >= SEARCH_LIMIT && (
        <div className="submeta">
          top {SEARCH_LIMIT} {browse ? 'threads' : 'hits'} shown — narrow the{' '}
          {browse ? 'window or filters' : 'query or filters'} to see the rest
        </div>
      )}
      {browse &&
        hits &&
        hits.map((h) => (
          // event_id is the thread's newest event: open the thread at its tail,
          // where the recent activity that ranked it here actually is.
          <Link className="hit" key={h.thread_id} to={`/archive/${h.thread_id}?e=${h.event_id}`}>
            <div className="snip">{h.thread_title || 'thread ' + h.thread_id}</div>
            <div className="meta">
              {h.thread_source && <span className="badge">{h.thread_source}</span>}
              {h.n_events != null && (
                <span className="badge">
                  {h.n_events} event{h.n_events === 1 ? '' : 's'}
                </span>
              )}
              {h.occurred_at && <span className="badge">{fmtDate(h.occurred_at)}</span>}
            </div>
          </Link>
        ))}
      {!browse &&
        hits &&
        group(hits).map((g) => (
          <div className="group" key={g.threadId}>
            <Link className="gh" to={'/archive/' + g.threadId}>
              <span>{g.title || 'thread ' + g.threadId}</span>
              <span className="src">
                #{g.threadId} · {g.hits.length} hit{g.hits.length > 1 ? 's' : ''}
              </span>
            </Link>
            {g.hits.map((h) => (
              // The dup disclosure is a sibling of the hit link, not a child:
              // an expander nested inside an <a> would be an anchor in an anchor.
              <div key={h.event_id}>
                <Link className="hit" to={`/archive/${g.threadId}?e=${h.event_id}`}>
                  <div className="snip">{h.snippet || h.full_content.slice(0, 280)}</div>
                  <div className="meta">
                    {h.term_hits != null && quality && (
                      <span className={'badge' + (h.term_hits === 0 ? ' sem' : '')} title="query terms matched">
                        {h.term_hits}/{quality.n_terms}
                      </span>
                    )}
                    {h.content_type && <span className="badge">{h.content_type}</span>}
                    {h._semantic != null && <span className="badge sem">semantic</span>}
                    {h.occurred_at && <span className="badge">{fmtDate(h.occurred_at)}</span>}
                  </div>
                </Link>
                {h.dup_threads && h.dup_threads.length > 0 && (
                  <div className="dups">
                    <button
                      type="button"
                      className="dups-toggle"
                      aria-expanded={openDups.has(h.event_id)}
                      onClick={() =>
                        setOpenDups((prev) => {
                          const next = new Set(prev)
                          if (!next.delete(h.event_id)) next.add(h.event_id)
                          return next
                        })
                      }
                    >
                      {openDups.has(h.event_id) ? '▾' : '▸'} same text in {h.dup_threads.length} other
                      thread{h.dup_threads.length === 1 ? '' : 's'}
                    </button>
                    {openDups.has(h.event_id) && (
                      <div className="dups-list">
                        {h.dup_threads.map((d) => (
                          <Link className="dup" key={d.thread_id} to={'/archive/' + d.thread_id}>
                            {d.title || 'thread ' + d.thread_id}
                            <span className="src">#{d.thread_id}</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        ))}
    </div>
  )
}
