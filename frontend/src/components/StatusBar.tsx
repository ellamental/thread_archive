import { Link, useLocation } from 'react-router-dom'

function sectionLabel(pathname: string): string {
  if (pathname === '/') return 'Home'
  if (pathname === '/search') return 'Search'
  if (pathname === '/threads') return 'Browse'
  if (pathname === '/stats') return 'Stats'
  if (pathname.startsWith('/stats/model/')) return 'Model stats'
  if (pathname.startsWith('/archive/')) return 'Conversation'
  return 'Archive'
}

// The shell header names where the reader is and keeps search one keystroke
// away. Corpus/index telemetry belongs on Stats rather than occupying the most
// prominent line of every page.
export function StatusBar({
  sidebarOpen = false,
  onOpenSidebar,
  onSearch,
}: {
  sidebarOpen?: boolean
  onOpenSidebar?: () => void
  onSearch?: () => void
}) {
  const { pathname } = useLocation()

  return (
    <header className="appbar">
      <button
        className="sidebar-toggle"
        aria-label="open navigation"
        aria-controls="archive-navigation"
        aria-expanded={sidebarOpen}
        onClick={onOpenSidebar}
      >
        ☰
      </button>
      <div className="appbar-context">
        <Link className="appbar-home" to="/">Archive</Link>
        <span className="appbar-separator">/</span>
        <span>{sectionLabel(pathname)}</span>
      </div>
      <button className="search-shortcut" type="button" onClick={onSearch}>
        <span>Search</span>
        <kbd>/</kbd>
      </button>
    </header>
  )
}
