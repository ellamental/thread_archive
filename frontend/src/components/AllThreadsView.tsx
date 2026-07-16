import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, type ThreadListItem, type ThreadTypeCount } from '../api'

// One page of the newest threads — the server clamps /api/threads to 500, so
// asking for more silently caps there anyway.
export const ALL_THREADS_LIMIT = 500

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
  const [threads, setThreads] = useState<ThreadListItem[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const hidden = useMemo(
    () => new Set((params.get('hide') ?? '').split(',').filter(Boolean)),
    [params],
  )

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
      setThreads([])
      return
    }
    let live = true
    api
      .threads({ types: visible, limit: ALL_THREADS_LIMIT, q: filter.trim() || undefined })
      .then((t) => live && setThreads(t))
      .catch((e) => live && setErr(String(e.message ?? e)))
    return () => {
      live = false
    }
  }, [types, visible, filter])

  function toggle(t: string) {
    const next = new Set(hidden)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    setParams(next.size ? { hide: [...next].sort().join(',') } : {}, { replace: true })
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
          onChange={(e) => setFilter(e.target.value)}
          autoComplete="off"
        />
      </div>
      {threads === null && <div className="empty">loading…</div>}
      {threads !== null && (
        <>
          <div className="submeta">
            {threads.length >= ALL_THREADS_LIMIT
              ? `newest ${ALL_THREADS_LIMIT.toLocaleString()} shown — hide types or filter to reach older threads`
              : `${threads.length.toLocaleString()} thread${threads.length === 1 ? '' : 's'}`}
          </div>
          {threads.map((t) => (
            <Link
              className="hit"
              key={t.id}
              to={(t.thread_type === 'topic' ? '/topic/' : '/archive/') + t.id}
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
          {threads.length === 0 && (
            <div className="empty">
              {visible.length === 0 ? 'every type is hidden' : 'no matches'}
            </div>
          )}
        </>
      )}
    </div>
  )
}
