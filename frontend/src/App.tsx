import { useState } from 'react'
import { Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { SearchView } from './components/SearchView'
import { ThreadView } from './components/ThreadView'
import { AllThreadsView } from './components/AllThreadsView'
import { StatsView } from './components/StatsView'
import { ModelStatsView } from './components/ModelStatsView'
import { Landing } from './components/Landing'

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false)

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
        />
        <div className="content">
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/search" element={<SearchView />} />
            <Route path="/threads" element={<AllThreadsView />} />
            <Route path="/stats" element={<StatsView />} />
            <Route path="/stats/model/:model" element={<ModelStatsView />} />
            <Route path="/archive/:id" element={<ThreadView />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
