import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, type ThreadPage, type ThreadTypeCount } from '../api'

export const ALL_THREADS_PAGE_SIZE = 100

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime())
    ? ''
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

// The all-threads page: everything in the archive newest-first — including the
// system (subagent) and topic threads the sidebar's recent list hides — with a
// checkbox per thread type to hide it. The hidden set rides in the URL (?hide=)
// so a filtered view is shareable and back-navigable; nothing hidden = all shown.
export function AllThreadsView() {
  const [params, setParams] = useSearchParams()
  const [types, setTypes] = useState<ThreadTypeCount[] | null>(null)
  const [result, setResult] = useState<ThreadPage | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const hidden = useMemo(
    () => new Set((params.get('hide') ?? '').split(',').filter(Boolean)),
    [params],
  )
  const parsedPage = Number(params.get('page') ?? '1')
  const page = Number.isInteger(parsedPage) && parsedPage > 0 ? parsedPage : 1

  useEffect(() => {
    api.threadTypes().then(setTypes).catch((e) => setErr(String(e.message ?? e)))
  }, [])

  // The types still shown. The threads fetch always sends this list explicitly:
  // with no types param the server applies its sidebar default (topics and
  // system runs hidden), which is exactly what this page exists to override.
  const visible = useMemo(
    () => (types ?? []).map((t) => t.thread_type).filter((t) => !hidden.has(t)),
    [types, hidden],
  )

  useEffect(() => {
    if (!types) return
    if (visible.length === 0) {
      setResult({
        threads: [],
        total: 0,
        page: 1,
        page_size: ALL_THREADS_PAGE_SIZE,
        pages: 0,
      })
      return
    }
    let live = true
    setResult(null)
    setErr(null)
    api
      .threadPage({
        types: visible,
        limit: ALL_THREADS_PAGE_SIZE,
        page,
        q: filter.trim() || undefined,
      })
      .then((data) => {
        if (!live) return
        if (data.pages > 0 && page > data.pages) {
          const next = new URLSearchParams(params)
          if (data.pages === 1) next.delete('page')
          else next.set('page', String(data.pages))
          setParams(next, { replace: true })
          return
        }
        setResult(data)
      })
      .catch((e) => live && setErr(String(e.message ?? e)))
    return () => {
      live = false
    }
  }, [types, visible, filter, page, params, setParams])

  function toggle(t: string) {
    const next = new Set(hidden)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    const nextParams = new URLSearchParams(params)
    if (next.size) nextParams.set('hide', [...next].sort().join(','))
    else nextParams.delete('hide')
    nextParams.delete('page')
    setParams(nextParams, { replace: true })
  }

  function goToPage(nextPage: number) {
    const next = new URLSearchParams(params)
    if (nextPage <= 1) next.delete('page')
    else next.set('page', String(nextPage))
    setParams(next)
  }

  function updateFilter(value: string) {
    setFilter(value)
    if (page !== 1) {
      const next = new URLSearchParams(params)
      next.delete('page')
      setParams(next, { replace: true })
    }
  }

  function pager(position: 'top' | 'bottom') {
    if (!result || result.pages <= 1) return null
    return (
      <nav className="thread-pagination" aria-label={`thread pages ${position}`}>
        <button
          className="toolbar-btn"
          disabled={result.page === 1}
          onClick={() => goToPage(1)}
        >
          First
        </button>
        <button
          className="toolbar-btn"
          disabled={result.page === 1}
          onClick={() => goToPage(result.page - 1)}
        >
          Previous
        </button>
        <span>
          Page {result.page.toLocaleString()} of {result.pages.toLocaleString()}
        </span>
        <button
          className="toolbar-btn"
          disabled={result.page === result.pages}
          onClick={() => goToPage(result.page + 1)}
        >
          Next
        </button>
        <button
          className="toolbar-btn"
          disabled={result.page === result.pages}
          onClick={() => goToPage(result.pages)}
        >
          Last
        </button>
      </nav>
    )
  }

  if (err) return <div className="empty">threads unavailable: {err}</div>
  if (!types) return <div className="empty">loading…</div>

  return (
    <div className="wrap">
      <div className="type-filters">
        {types.map((t) => (
          <label className="type-filter" key={t.thread_type}>
            <input
              type="checkbox"
              checked={!hidden.has(t.thread_type)}
              onChange={() => toggle(t.thread_type)}
            />
            <span>{t.thread_type}</span>
            {t.thread_type === 'system' && <span className="src">subagents</span>}
            <span className="src">{t.threads.toLocaleString()}</span>
          </label>
        ))}
      </div>
      <div className="topics-filter">
        <input
          className="input"
          type="search"
          placeholder="filter by title…"
          value={filter}
          onChange={(e) => updateFilter(e.target.value)}
          autoComplete="off"
        />
      </div>
      {result === null && <div className="empty">loading…</div>}
      {result !== null && (
        <>
          <div className="submeta">
            {result.total.toLocaleString()} thread{result.total === 1 ? '' : 's'}
            {result.pages > 0 &&
              ` · page ${result.page.toLocaleString()} of ${result.pages.toLocaleString()}`}
          </div>
          {pager('top')}
          {result.threads.map((t) => (
            <Link
              className="hit"
              key={t.id}
              to={'/archive/' + t.id}
            >
              <div className="snip">
                <strong>{t.title || 'thread ' + t.id}</strong>
              </div>
              <div className="meta">
                <span className="badge">{t.thread_type}</span>
                {t.source && <span className="badge">{t.source}</span>}
                {t.updated_at && <span className="badge">{fmtDate(t.updated_at)}</span>}
              </div>
            </Link>
          ))}
          {pager('bottom')}
          {result.threads.length === 0 && (
            <div className="empty">
              {visible.length === 0 ? 'every type is hidden' : 'no matches'}
            </div>
          )}
        </>
      )}
    </div>
  )
}
