import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, type SearchHit, type SearchResponse } from '../api'
import { Pager } from './Pager'
import { SearchBox } from './SearchBox'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

interface Group {
  threadId: string
  title: string | null
  source: string | null
  hits: SearchHit[]
}

function group(hits: SearchHit[]): Group[] {
  const out: Group[] = []
  const byId = new Map<string, Group>()
  for (const h of hits) {
    let g = byId.get(h.thread_id)
    if (!g) {
      g = { threadId: h.thread_id, title: h.thread_title, source: h.thread_source ?? null, hits: [] }
      byId.set(h.thread_id, g)
      out.push(g)
    }
    g.hits.push(h)
  }
  return out
}

const qualityLabels = {
  strong: 'good match',
  partial: 'mixed match',
  weak: 'weak match',
  semantic: 'meaning-based match',
} as const

// Where this page sits in the match set, in the words the MCP renderer uses for
// the same search — the two surfaces read one archive and must not describe it
// differently. `of` says paging can reach every match; `≥` says the ranked walk
// stopped at the pool it scored, so the total is real and the walk is what falls
// short. A trailing `+` marks a total the set scan capped, which is a floor.
function scale(resp: SearchResponse, one: string, many: string): string | null {
  const total = one === 'thread' ? (resp.total_threads ?? resp.total) : resp.total
  if (total == null) return null
  const mark = resp.capped ? '+' : ''
  const reach = resp.exhaustive ? '' : '≥'
  const noun = total === 1 && !mark && !reach ? one : many
  return `${resp.hits.length.toLocaleString()} of ${reach}${total.toLocaleString()}${mark} ${noun}`
}

function hitUrl(g: Group, eventId: number, query: string): string {
  const p = new URLSearchParams({ e: String(eventId) })
  if (query) p.set('q', query)
  const events = [...new Set(g.hits.map((hit) => hit.event_id))]
  if (events.length > 1) p.set('hits', events.join(','))
  return `/archive/${g.threadId}?${p.toString()}`
}

export function SearchView() {
  // Query and filters both live in the URL (the sidebar's search widget writes
  // them), so navigating into a thread and coming back — or sharing the
  // address — restores the exact same result page. An empty query is a browse:
  // recent threads, one row each, honoring the same filters.
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const source = params.get('source') ?? ''
  const since = params.get('since') ?? ''
  const until = params.get('until') ?? ''
  // The page rides the URL like the query and the filters do, so a result page
  // deep in a walk is shareable and survives opening a thread and coming back.
  // A garbled ?page= reads as the first page rather than as no results.
  const parsedPage = Number(params.get('page') ?? '1')
  const page = Number.isInteger(parsedPage) && parsedPage > 0 ? parsedPage : 1
  const [resp, setResp] = useState<SearchResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const browse = !q

  useEffect(() => {
    setResp(null)
    setErr(null)
    // Stale guard: only the latest request may land — without it a slow older
    // search can resolve after a newer one and silently replace its results.
    let live = true
    // A bare `until` date resolves server-side to that day's midnight, which
    // excludes the day itself; a "to" filter should include the chosen day.
    api
      .search(
        q,
        {
          source: source || undefined,
          since: since || undefined,
          until: until ? until + 'T23:59:59' : undefined,
        },
        page,
      )
      .then((r) => {
        if (!live) return
        // A page past the end of a set that has one — a narrowed filter, a
        // shared link into a shrunken archive — lands on the last real page
        // instead of on a blank one the reader has to guess their way out of.
        if (r.pages != null && r.pages > 0 && page > r.pages) {
          goToPage(r.pages, { replace: true })
          return
        }
        setResp(r)
      })
      .catch((e) => live && setErr(String(e.message ?? e)))
    return () => {
      live = false
    }
  }, [q, source, since, until, page])

  function goToPage(next: number, opts?: { replace?: boolean }) {
    const nextParams = new URLSearchParams(params)
    if (next <= 1) nextParams.delete('page')
    else nextParams.set('page', String(next))
    setParams(nextParams, { replace: opts?.replace ?? false })
  }

  const filters = [source, since && `from ${since}`, until && `to ${until}`].filter(Boolean)
  const hits = resp?.hits ?? null
  const quality = resp?.quality
  const subjects = resp?.subjects ?? []
  const position = resp ? scale(resp, browse ? 'thread' : 'match', browse ? 'threads' : 'matches') : null
  const pages = resp?.pages ?? 0

  return (
    <div className="wrap">
      <div className="page-search">
        <SearchBox variant="page" primary />
      </div>
      <div className="submeta">
        {browse ? 'recent threads — newest activity first' : `results for “${q}”`}
        {filters.length > 0 && ` · ${filters.join(' · ')}`}
        {position && ` · ${position}`}
        {pages > 1 && ` · page ${page.toLocaleString()} of ${pages.toLocaleString()}`}
        {quality && (
          <>
            {' · '}
            <span
              className={'badge quality-' + quality.verdict}
              title="How closely the top results match the words and meaning of this search"
            >
              {qualityLabels[quality.verdict]}
            </span>
          </>
        )}
      </div>
      {quality?.note && <div className="quality-note">{quality.note}</div>}
      {subjects.length > 0 && (
        <div className="subjects">
          <span className="subjects-label">common subjects:</span>
          {subjects.map((s) => (
            <span
              className="subject-tag"
              key={s.topic_id}
              title={`Appears in ${s.chats} result conversation${s.chats === 1 ? '' : 's'}`}
            >
              {s.title} ({s.chats})
            </span>
          ))}
        </div>
      )}
      {err && <div className="empty">search error: {err}</div>}
      {!err && hits === null && <div className="empty">{browse ? 'loading…' : 'searching…'}</div>}
      {!err && hits && hits.length === 0 && (
        <div className="empty">{browse ? 'no threads in this window' : 'no matches'}</div>
      )}
      {/* A ranked walk reaches pool-deep and no further, and the pages it offers
          are the ones that return rows — so the matches past them are reachable
          only by asking a narrower question. Said once, where the count that
          provoked it is, rather than left for the reader to discover at the last
          page. */}
      {resp && resp.exhaustive === false && (
        <div className="quality-note">
          paging reaches the best-scoring matches — narrow the{' '}
          {browse ? 'window or filters' : 'query or filters'} to reach the rest
        </div>
      )}
      {hits && hits.length > 0 && (
        <Pager page={page} pages={pages} label="result pages" position="top" onGo={goToPage} />
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
                {[g.source, `${g.hits.length} hit${g.hits.length > 1 ? 's' : ''}`]
                  .filter(Boolean)
                  .join(' · ')}
              </span>
            </Link>
            {g.hits.map((h) => (
              <div key={h.event_id}>
                <Link className="hit" to={hitUrl(g, h.event_id, q)}>
                  <div className="snip">{h.snippet || h.full_content.slice(0, 280)}</div>
                  <div className="meta">
                    {h.term_hits != null && quality && (
                      <span
                        className={'badge' + (h.term_hits === 0 ? ' sem' : '')}
                        title="Search words found in this result"
                      >
                        {h.term_hits} of {quality.n_terms} words
                      </span>
                    )}
                    {h.content_type && <span className="badge">{h.content_type}</span>}
                    {h._semantic != null && (
                      <span className="badge sem" title="Found by similarity in meaning">
                        meaning match
                      </span>
                    )}
                    {h.occurred_at && <span className="badge">{fmtDate(h.occurred_at)}</span>}
                  </div>
                </Link>
              </div>
            ))}
          </div>
        ))}
      {hits && hits.length > 0 && (
        <Pager page={page} pages={pages} label="result pages" position="bottom" onGo={goToPage} />
      )}
    </div>
  )
}
