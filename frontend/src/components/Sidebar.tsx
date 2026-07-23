import { useEffect, useState, type KeyboardEvent } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type SourceCount, type ThreadListItem } from '../api'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime())
    ? ''
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function Sidebar({
  open = false,
  onClose,
}: {
  open?: boolean
  onClose?: () => void
}) {
  const navigate = useNavigate()
  const { id } = useParams()
  const { pathname } = useLocation()
  const [params] = useSearchParams()
  // Rail items carry canonical ULID ids, so only a ULID address highlights one;
  // a not-yet-resolved ref (legacy integer, session uuid) highlights nothing.
  const activeId = id ?? null

  const [term, setTerm] = useState(params.get('q') ?? '')
  const [threads, setThreads] = useState<ThreadListItem[]>([])
  const [filter, setFilter] = useState('')

  // Search filters ride with the widget (they render on every page), initialized
  // from the URL so a shared/back-navigated /search address shows its own filters.
  const [showFilters, setShowFilters] = useState(false)
  const [sources, setSources] = useState<SourceCount[]>([])
  const [source, setSource] = useState(params.get('source') ?? '')
  const [since, setSince] = useState(params.get('since') ?? '')
  const [until, setUntil] = useState(params.get('until') ?? '')
  const activeFilters = [source, since, until].filter(Boolean).length

  useEffect(() => {
    api.sources().then(setSources).catch(() => {})
  }, [])

  useEffect(() => {
    let live = true
    api.threads({ q: filter || undefined }).then((t) => live && setThreads(t)).catch(() => {})
    return () => {
      live = false
    }
  }, [filter])

  function searchUrl(q: string, f: { source: string; since: string; until: string }): string {
    const p = new URLSearchParams()
    if (q) p.set('q', q)
    if (f.source) p.set('source', f.source)
    if (f.since) p.set('since', f.since)
    if (f.until) p.set('until', f.until)
    const qs = p.toString()
    return '/search' + (qs ? '?' + qs : '')
  }

  // Enter always lands on /search: with a query it searches, empty it browses
  // (recent threads under the armed filters).
  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return
    navigate(searchUrl(term.trim(), { source, since, until }))
    onClose?.()
  }

  // A filter change applies immediately when a search (or browse) is already on
  // screen; otherwise it just sits armed until Enter runs one.
  function applyFilter(next: { source: string; since: string; until: string }) {
    setSource(next.source)
    setSince(next.since)
    setUntil(next.until)
    const q = (params.get('q') ?? term).trim()
    if (pathname === '/search') navigate(searchUrl(q, next), { replace: true })
  }

  // Keep a URL-carried source selectable even if /api/sources failed or lags.
  const sourceMissing = source && !sources.some((s) => s.source === source)

  return (
    <aside id="archive-navigation" className={'sidebar' + (open ? ' open' : '')}>
      <div className="brand">
        <span>thread-archive</span>
        <button className="sidebar-close" aria-label="close navigation" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="searchbar">
        <input
          className="input"
          type="search"
          placeholder="search conversations…"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          onKeyDown={onSearchKey}
          autoComplete="off"
        />
        <button
          className={'filters-toggle' + (activeFilters > 0 ? ' on' : '')}
          aria-expanded={showFilters}
          onClick={() => setShowFilters((v) => !v)}
        >
          {showFilters ? '▾' : '▸'} filters{activeFilters > 0 ? ` · ${activeFilters}` : ''}
        </button>
        {showFilters && (
          <div className="filters-panel">
            <select
              className="filter-input"
              value={source}
              aria-label="source"
              onChange={(e) => applyFilter({ source: e.target.value, since, until })}
            >
              <option value="">all sources</option>
              {sourceMissing && <option value={source}>{source}</option>}
              {sources.map((s) => (
                <option key={s.source} value={s.source}>
                  {s.source} ({s.threads.toLocaleString()})
                </option>
              ))}
            </select>
            <label className="filter-date">
              from{' '}
              <input
                className="filter-input"
                type="date"
                aria-label="since"
                value={since}
                onChange={(e) => applyFilter({ source, since: e.target.value, until })}
              />
            </label>
            <label className="filter-date">
              to{' '}
              <input
                className="filter-input"
                type="date"
                aria-label="until"
                value={until}
                onChange={(e) => applyFilter({ source, since, until: e.target.value })}
              />
            </label>
            {activeFilters > 0 && (
              <button
                className="filters-clear"
                onClick={() => applyFilter({ source: '', since: '', until: '' })}
              >
                clear filters
              </button>
            )}
          </div>
        )}
      </div>
      <nav className="rail-nav">
        <Link
          className={'rail-link' + (pathname === '/threads' ? ' active' : '')}
          to="/threads"
          onClick={onClose}
        >
          all threads
        </Link>
        <Link
          className={'rail-link' + (pathname === '/stats' ? ' active' : '')}
          to="/stats"
          onClick={onClose}
        >
          stats
        </Link>
      </nav>
      <div className="rail-head">
        <span>Recent</span>
        <input
          className="rail-filter"
          type="search"
          placeholder="filter…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div className="rail">
        {threads.map((t) => (
          <Link
            key={t.id}
            className={'rail-item' + (t.id === activeId ? ' active' : '')}
            to={'/archive/' + t.id}
            onClick={onClose}
          >
            <span className="t">{t.title || 'thread ' + t.id}</span>
            <span className="m">{[t.source, fmtDate(t.updated_at)].filter(Boolean).join(' · ')}</span>
          </Link>
        ))}
        {threads.length === 0 && <div className="rail-empty">no conversations</div>}
      </div>
    </aside>
  )
}
