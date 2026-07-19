import { Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { SearchView } from './components/SearchView'
import { ThreadView } from './components/ThreadView'
import { TopicsView } from './components/TopicsView'
import { TopicView } from './components/TopicView'
import { AllThreadsView } from './components/AllThreadsView'
import { StatsView } from './components/StatsView'
import { ModelStatsView } from './components/ModelStatsView'
import { CurationView } from './components/CurationView'
import { Landing } from './components/Landing'

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
            <Route path="/topics" element={<TopicsView />} />
            <Route path="/topic/:id" element={<TopicView />} />
            <Route path="/threads" element={<AllThreadsView />} />
            <Route path="/stats" element={<StatsView />} />
            <Route path="/stats/model/:model" element={<ModelStatsView />} />
            <Route path="/curation" element={<CurationView />} />
            <Route path="/archive/:id" element={<ThreadView />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
