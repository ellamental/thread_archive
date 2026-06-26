import { Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { SearchView } from './components/SearchView'
import { ThreadView } from './components/ThreadView'
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
            <Route path="/archive/:id" element={<ThreadView />} />
          </Routes>
        </div>
      </main>
    </div>
  )
}
