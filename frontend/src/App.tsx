import { useCallback, useEffect, useState } from 'react'
import { Routes, Route, useLocation } from 'react-router-dom'
import { devPanels } from './dev'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { SearchView } from './components/SearchView'
import { ThreadView } from './components/ThreadView'
import { AllThreadsView } from './components/AllThreadsView'
import { StatsView } from './components/StatsView'
import { ModelStatsView } from './components/ModelStatsView'
import { HealthView } from './components/HealthView'
import { RetrievalView } from './components/RetrievalView'
import { TelemetryView } from './components/TelemetryView'
import { SearchLabView } from './components/SearchLabView'
import { BenchRunView } from './components/BenchRunView'
import { UploadView } from './components/UploadView'
import { Landing } from './components/Landing'

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { pathname } = useLocation()
  // Read once per mount: the served shell decides it, so it cannot change
  // without a page load.
  const [dev] = useState(devPanels)

  const focusSearch = useCallback(() => {
    const visibleSearch = (selector: string) =>
      [...document.querySelectorAll<HTMLInputElement>(selector)].find(
        (input) => input.getClientRects().length > 0,
      )

    // The home hero is the primary search surface. On every other desktop page,
    // use the rail. A closed mobile rail opens before its input receives focus.
    const primary = document.querySelector<HTMLInputElement>(
      '[data-global-search][data-primary="true"]',
    )
    if (primary) {
      primary.focus()
      return
    }
    if (window.matchMedia?.('(max-width: 759px)').matches) {
      setSidebarOpen(true)
      window.setTimeout(() => {
        document
          .querySelector<HTMLInputElement>('#archive-navigation [data-global-search]')
          ?.focus()
      }, 0)
      return
    }
    visibleSearch('[data-global-search]')?.focus()
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing =
        target?.matches('input, textarea, select') || target?.isContentEditable
      if (typing || (!(event.metaKey || event.ctrlKey) && event.key !== '/')) return
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() !== 'k') return
      event.preventDefault()
      focusSearch()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [focusSearch])

  useEffect(() => {
    setSidebarOpen(false)
  }, [pathname])

  return (
    <div className="app">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <main>
        <StatusBar
          sidebarOpen={sidebarOpen}
          onOpenSidebar={() => setSidebarOpen(true)}
          onSearch={focusSearch}
        />
        <div className="content">
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/search" element={<SearchView />} />
            <Route path="/threads" element={<AllThreadsView />} />
            <Route path="/stats" element={<StatsView />} />
            <Route path="/stats/model/:model" element={<ModelStatsView />} />
            <Route path="/health" element={<HealthView />} />
            {/* Dev panels: in the bundle always, mounted only for a viewer whose
                operator asked for them (see src/dev.ts). Without that, these
                addresses route nowhere, like any other path the app lacks. */}
            {dev && (
              <>
                <Route path="/retrieval" element={<RetrievalView />} />
                <Route path="/telemetry" element={<TelemetryView />} />
                <Route path="/lab" element={<SearchLabView />} />
                <Route path="/lab/run/:id" element={<BenchRunView />} />
              </>
            )}
            <Route path="/upload" element={<UploadView />} />
            <Route path="/archive/:id" element={<ThreadView />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
