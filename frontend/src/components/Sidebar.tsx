import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { api, type ThreadListItem } from '../api'
import { DEV_PANELS_URL, devPanels } from '../dev'
import { SearchBox } from './SearchBox'

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
  const { id } = useParams()
  const { pathname } = useLocation()
  // Rail items carry canonical ULID ids, so only a ULID address highlights one;
  // a not-yet-resolved ref (legacy integer, session uuid) highlights nothing.
  const activeId = id ?? null

  const [threads, setThreads] = useState<ThreadListItem[] | null>(null)
  // Read once per mount: the served shell decides it, which is a page load anyway.
  const [dev] = useState(devPanels)

  useEffect(() => {
    let live = true
    api.threads().then((items) => live && setThreads(items)).catch(() => live && setThreads([]))
    return () => {
      live = false
    }
  }, [])

  return (
    <aside id="archive-navigation" className={'sidebar' + (open ? ' open' : '')}>
      <div className="brand">
        <Link to="/" onClick={onClose}>thread-archive</Link>
        <button className="sidebar-close" aria-label="close navigation" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="searchbar">
        <SearchBox variant="compact" onNavigate={onClose} />
      </div>
      <nav className="rail-nav">
        <Link
          className={'rail-link' + (pathname === '/threads' ? ' active' : '')}
          to="/threads"
          onClick={onClose}
        >
          browse
        </Link>
        <Link
          className={'rail-link' + (pathname === '/stats' ? ' active' : '')}
          to="/stats"
          onClick={onClose}
        >
          stats
        </Link>
        <Link
          className={'rail-link' + (pathname === '/health' ? ' active' : '')}
          to="/health"
          onClick={onClose}
        >
          health
        </Link>
        <Link
          className={'rail-link' + (pathname === '/upload' ? ' active' : '')}
          to="/upload"
          onClick={onClose}
        >
          import
        </Link>
        <Link
          // Every page under /docs is the manual, so the rail stays lit while
          // reading one — unlike the leaf routes above, which are single pages.
          className={'rail-link' + (pathname.startsWith('/docs') ? ' active' : '')}
          to="/docs"
          onClick={onClose}
        >
          docs
        </Link>
        {dev && (
          // A plain anchor, not a Link: the panels are a different origin, and
          // client-side routing to them would ask this app for a page it does
          // not have. Marked as leaving, because it is.
          <a className="rail-link rail-away" href={DEV_PANELS_URL} onClick={onClose}>
            dev panels ↗
          </a>
        )}
      </nav>
      <div className="rail-head">
        <span>Recent</span>
      </div>
      <div className="rail">
        {threads?.map((t) => (
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
        {threads === null && <div className="rail-empty">loading…</div>}
        {threads?.length === 0 && <div className="rail-empty">no conversations</div>}
      </div>
    </aside>
  )
}
