import { useEffect, useState, type FormEvent } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { api, type SourceCount } from '../api'

type SearchBoxVariant = 'compact' | 'hero' | 'page'

export function SearchBox({
  variant = 'compact',
  primary = false,
  onNavigate,
}: {
  variant?: SearchBoxVariant
  primary?: boolean
  onNavigate?: () => void
}) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [params] = useSearchParams()
  const [term, setTerm] = useState(params.get('q') ?? '')
  const [source, setSource] = useState(params.get('source') ?? '')
  const [since, setSince] = useState(params.get('since') ?? '')
  const [until, setUntil] = useState(params.get('until') ?? '')
  const [sources, setSources] = useState<SourceCount[]>([])
  const [showFilters, setShowFilters] = useState(variant !== 'compact')
  const activeFilters = [source, since, until].filter(Boolean).length

  useEffect(() => {
    api.sources().then(setSources).catch(() => {})
  }, [])

  // Back/forward navigation and links shared from another browser are
  // authoritative. Keep every copy of the search surface synchronized to them.
  useEffect(() => {
    setTerm(params.get('q') ?? '')
    setSource(params.get('source') ?? '')
    setSince(params.get('since') ?? '')
    setUntil(params.get('until') ?? '')
  }, [params])

  function searchUrl(
    q: string,
    filters: { source: string; since: string; until: string },
  ): string {
    const next = new URLSearchParams()
    if (q) next.set('q', q)
    if (filters.source) next.set('source', filters.source)
    if (filters.since) next.set('since', filters.since)
    if (filters.until) next.set('until', filters.until)
    const query = next.toString()
    return '/search' + (query ? '?' + query : '')
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    navigate(searchUrl(term.trim(), { source, since, until }))
    onNavigate?.()
  }

  // Filters on the results page are live facets. Elsewhere they stay armed
  // until the search is submitted, avoiding an unexpected page transition.
  function applyFilters(next: { source: string; since: string; until: string }) {
    setSource(next.source)
    setSince(next.since)
    setUntil(next.until)
    if (pathname === '/search') {
      navigate(searchUrl((params.get('q') ?? term).trim(), next), { replace: true })
    }
  }

  const sourceMissing = source && !sources.some((item) => item.source === source)

  return (
    <form className={`archive-search ${variant}`} role="search" onSubmit={submit}>
      <div className="search-row">
        <input
          className="input search-input"
          type="search"
          aria-label="search conversations"
          placeholder="search conversations…"
          value={term}
          onChange={(event) => setTerm(event.target.value)}
          autoComplete="off"
          data-global-search
          data-primary={primary ? 'true' : undefined}
        />
        <button className="search-submit" type="submit">
          Search
        </button>
      </div>
      {variant === 'compact' && (
        <button
          className={'filters-toggle' + (activeFilters > 0 ? ' on' : '')}
          type="button"
          aria-expanded={showFilters}
          onClick={() => setShowFilters((visible) => !visible)}
        >
          {showFilters ? '▾' : '▸'} filters{activeFilters > 0 ? ` · ${activeFilters}` : ''}
        </button>
      )}
      {showFilters && (
        <div className="filters-panel">
          <label className="filter-field source-filter">
            <span>Source</span>
            <select
              className="filter-input"
              value={source}
              aria-label="source"
              onChange={(event) =>
                applyFilters({ source: event.target.value, since, until })
              }
            >
              <option value="">all sources</option>
              {sourceMissing && <option value={source}>{source}</option>}
              {sources.map((item) => (
                <option key={item.source} value={item.source}>
                  {item.source} ({item.threads.toLocaleString()})
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span>From</span>
            <input
              className="filter-input"
              type="date"
              aria-label="since"
              value={since}
              onChange={(event) =>
                applyFilters({ source, since: event.target.value, until })
              }
            />
          </label>
          <label className="filter-field">
            <span>To</span>
            <input
              className="filter-input"
              type="date"
              aria-label="until"
              value={until}
              onChange={(event) =>
                applyFilters({ source, since, until: event.target.value })
              }
            />
          </label>
          {activeFilters > 0 && (
            <button
              className="filters-clear"
              type="button"
              onClick={() => applyFilters({ source: '', since: '', until: '' })}
            >
              clear filters
            </button>
          )}
        </div>
      )}
    </form>
  )
}
