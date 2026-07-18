// The sidebar's search widget: the filters fold-out (available on every page,
// since the sidebar is), arming filters for the next search, and applying a
// filter change immediately when a search is already on screen.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { mswJson } from './msw'

// Echoes where the router landed, so tests assert the full search URL.
function PageStub() {
  const loc = useLocation()
  return <div>PAGE {loc.pathname + loc.search}</div>
}

function renderAt(url: string) {
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [{ source: 'cloth', threads: 3 }, { source: 'codex', threads: 1 }] })
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Sidebar />
      <Routes>
        <Route path="*" element={<PageStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Sidebar search filters', () => {
  it('arms filters in the fold-out and carries them into the next search', async () => {
    const user = userEvent.setup()
    renderAt('/')
    await user.click(screen.getByRole('button', { name: /filters/ }))
    await user.selectOptions(await screen.findByLabelText('source'), 'cloth')
    // not on /search yet: nothing navigates until a query is run
    expect(screen.getByText('PAGE /')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('search conversations…'), 'hello{Enter}')
    expect(screen.getByText('PAGE /search?q=hello&source=cloth')).toBeInTheDocument()
  })

  it('applies a filter change immediately when a search is on screen', async () => {
    const user = userEvent.setup()
    renderAt('/search?q=hello')
    await user.click(screen.getByRole('button', { name: /filters/ }))
    await user.selectOptions(await screen.findByLabelText('source'), 'codex')
    expect(screen.getByText('PAGE /search?q=hello&source=codex')).toBeInTheDocument()
  })

  it('browses on Enter with an empty query, carrying the armed filters', async () => {
    const user = userEvent.setup()
    renderAt('/')
    await user.click(screen.getByRole('button', { name: /filters/ }))
    await user.selectOptions(await screen.findByLabelText('source'), 'cloth')
    await user.type(screen.getByPlaceholderText('search conversations…'), '{Enter}')
    expect(screen.getByText('PAGE /search?source=cloth')).toBeInTheDocument()
  })

  it('applies a filter change immediately on an open browse (no query)', async () => {
    const user = userEvent.setup()
    renderAt('/search')
    await user.click(screen.getByRole('button', { name: /filters/ }))
    await user.selectOptions(await screen.findByLabelText('source'), 'codex')
    expect(screen.getByText('PAGE /search?source=codex')).toBeInTheDocument()
  })

  it('clears every filter at once and re-runs the open search', async () => {
    const user = userEvent.setup()
    renderAt('/search?q=hello&source=cloth&since=2026-01-01')
    // the toggle shows the active-filter count even before opening
    await user.click(screen.getByRole('button', { name: /filters · 2/ }))
    await user.click(screen.getByRole('button', { name: 'clear filters' }))
    expect(screen.getByText('PAGE /search?q=hello')).toBeInTheDocument()
  })
})
