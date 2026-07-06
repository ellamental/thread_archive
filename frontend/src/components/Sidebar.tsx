import { useEffect, useState, type KeyboardEvent } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, type ThreadListItem } from '../api'

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d.getTime())
    ? ''
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function Sidebar() {
  const navigate = useNavigate()
  const { id } = useParams()
  const [params] = useSearchParams()
  // Only a numeric id maps to a rail item; a not-yet-resolved uuid highlights nothing.
  const activeId = id && /^\d+$/.test(id) ? parseInt(id, 10) : null

  const [term, setTerm] = useState(params.get('q') ?? '')
  const [threads, setThreads] = useState<ThreadListItem[]>([])
  const [filter, setFilter] = useState('')

  useEffect(() => {
    let live = true
    api.threads(filter || undefined).then((t) => live && setThreads(t)).catch(() => {})
    return () => {
      live = false
    }
  }, [filter])

  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return
    const q = term.trim()
    navigate(q ? '/search?q=' + encodeURIComponent(q) : '/')
  }

  return (
    <aside>
      <div className="brand">thread-archive</div>
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
      </div>
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
          <button
            key={t.id}
            className={'rail-item' + (t.id === activeId ? ' active' : '')}
            onClick={() => navigate('/archive/' + t.id)}
          >
            <span className="t">{t.title || 'thread ' + t.id}</span>
            <span className="m">{[t.source, fmtDate(t.updated_at)].filter(Boolean).join(' · ')}</span>
          </button>
        ))}
        {threads.length === 0 && <div className="rail-empty">no conversations</div>}
      </div>
    </aside>
  )
}
