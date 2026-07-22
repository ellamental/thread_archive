import { Link, Navigate, Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { SearchView } from './components/SearchView'
import { ThreadView } from './components/ThreadView'
import { AllThreadsView } from './components/AllThreadsView'
import { StatsView } from './components/StatsView'
import { ModelStatsView } from './components/ModelStatsView'
import { PatternsView } from './components/PatternsView'
import { PatternDetailView } from './components/PatternDetailView'
import { Landing } from './components/Landing'

function ExperimentsView() {
  return (
    <div className="wrap">
      <h1 className="title">Experiments</h1>
      <p className="stat-note">Unstable derived analyses. These are local inspection tools, not Archive’s stable retrieval surface.</p>
      <div className="experiment-list">
        <Link to="/experiments/patterns">
          <strong>Behavioral pattern mining</strong>
          <span>Recurring bounded-gap event sequences with exact matching-thread drill-down.</span>
        </Link>
      </div>
    </div>
  )
}

export function App() {
  return (
    <div className="app">
      <Sidebar />
      <main>
        <StatusBar />
        <div className="content">
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/search" element={<SearchView />} />
            <Route path="/threads" element={<AllThreadsView />} />
            <Route path="/stats" element={<StatsView />} />
            <Route path="/stats/model/:model" element={<ModelStatsView />} />
            <Route path="/experiments" element={<ExperimentsView />} />
            <Route path="/experiments/patterns" element={<PatternsView />} />
            <Route path="/experiments/patterns/:patternId" element={<PatternDetailView />} />
            <Route path="/patterns" element={<Navigate to="/experiments/patterns" replace />} />
            <Route path="/archive/:id" element={<ThreadView />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
