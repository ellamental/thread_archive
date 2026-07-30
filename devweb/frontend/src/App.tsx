import { Link, Route, Routes, useLocation } from 'react-router-dom'

import { BenchRunView } from './components/BenchRunView'
import { RetrievalView } from './components/RetrievalView'
import { SearchLabView } from './components/SearchLabView'
import { TelemetryView } from './components/TelemetryView'

/**
 * The dev panels' shell.
 *
 * Deliberately thinner than the viewer's: no sidebar of recent threads, no
 * global search, no status bar. This app has no conversations in it — the
 * archive is next door at :8787 and that is where reading happens. What is left
 * is three instruments and a way between them.
 */

const NAV = [
  { to: '/retrieval', label: 'retrieval', blurb: 'how search is performing' },
  { to: '/telemetry', label: 'telemetry', blurb: 'the operational ledgers' },
  { to: '/lab', label: 'lab', blurb: 'what the bench can measure with' },
]

/** The archive's own viewer. Its port is fixed the same way this one's is. */
const VIEWER_URL = 'http://127.0.0.1:8787'

export function App() {
  const { pathname } = useLocation()

  return (
    <div className="devweb">
      <header className="devweb-head">
        <span className="devweb-brand">thread-archive · dev panels</span>
        <nav className="devweb-nav">
          {NAV.map((item) => (
            <Link
              key={item.to}
              // A run's own page is still the lab, so the nav keeps its mark: an
              // unlit link on a page reached from it reads as having left.
              className={
                'devweb-link' +
                (pathname === item.to || (item.to === '/lab' && pathname.startsWith('/lab'))
                  ? ' active'
                  : '')
              }
              to={item.to}
              title={item.blurb}
            >
              {item.label}
            </Link>
          ))}
          {/* A plain anchor, not a Link: the archive is a different origin. */}
          <a className="devweb-link devweb-away" href={VIEWER_URL}>
            archive ↗
          </a>
        </nav>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<SearchLabView />} />
          <Route path="/retrieval" element={<RetrievalView />} />
          <Route path="/telemetry" element={<TelemetryView />} />
          <Route path="/lab" element={<SearchLabView />} />
          <Route path="/lab/run/:id" element={<BenchRunView />} />
        </Routes>
      </main>
    </div>
  )
}
