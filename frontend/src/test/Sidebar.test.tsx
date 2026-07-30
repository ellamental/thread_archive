// The sidebar's search widget: the filters fold-out (available on every page,
// since the sidebar is), arming filters for the next search, and applying a
// filter change immediately when a search is already on screen.
import { afterEach, describe, expect, it } from 'vitest'
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
  mswJson('/api/sources', { sources: [{ source: 'demo-harness', threads: 3 }, { source: 'codex', threads: 1 }] })
  mswJson('/api/status', { threads: 0, events: 0, topics: 0, fts_indexed: 0, vectors_indexed: 0, home: '/tmp/a' })
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
  it('links to the user-visible trust center', () => {
    renderAt('/')
    expect(screen.getByRole('link', { name: 'health' })).toHaveAttribute('href', '/health')
  })

  it('arms filters in the fold-out and carries them into the next search', async () => {
    const user = userEvent.setup()
    renderAt('/')
    await user.click(screen.getByRole('button', { name: /filters/ }))
    await user.selectOptions(await screen.findByLabelText('source'), 'demo-harness')
    // not on /search yet: nothing navigates until a query is run
    expect(screen.getByText('PAGE /')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('search conversations…'), 'hello{Enter}')
    expect(screen.getByText('PAGE /search?q=hello&source=demo-harness')).toBeInTheDocument()
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
    await user.selectOptions(await screen.findByLabelText('source'), 'demo-harness')
    await user.type(screen.getByPlaceholderText('search conversations…'), '{Enter}')
    expect(screen.getByText('PAGE /search?source=demo-harness')).toBeInTheDocument()
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
    renderAt('/search?q=hello&source=demo-harness&since=2026-01-01')
    // the toggle shows the active-filter count even before opening
    await user.click(screen.getByRole('button', { name: /filters · 2/ }))
    await user.click(screen.getByRole('button', { name: 'clear filters' }))
    expect(screen.getByText('PAGE /search?q=hello')).toBeInTheDocument()
  })
})

// The retrieval report, telemetry and the lab are maintainer's instruments, not
// something a person who came to read their conversations has a use for. They
// are a different app on a different server now (devweb/, port 8789), and there
// is no switch that could put them back in this rail: the links, the routes and
// the components all left together.
describe('dev panels in the rail', () => {
  afterEach(() => {
    document.head
      .querySelectorAll('meta[name="thread-archive-dev-panels"]')
      .forEach((m) => m.remove())
  })

  function stampDevPanels(): void {
    const meta = document.createElement('meta')
    meta.setAttribute('name', 'thread-archive-dev-panels')
    meta.setAttribute('content', '1')
    document.head.appendChild(meta)
  }

  it('offers no way to the dev panels by default', () => {
    renderAt('/')
    expect(screen.queryByRole('link', { name: /dev panels/ })).not.toBeInTheDocument()
    // Nor the pages themselves, under any name: they are not this app's to route.
    expect(screen.queryByRole('link', { name: 'retrieval' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'telemetry' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'lab' })).not.toBeInTheDocument()
  })

  it('links out to the panels once the shell says the operator asked', () => {
    stampDevPanels()
    renderAt('/')
    // An absolute href, not a client route: the panels are a different origin,
    // so this leaves the app rather than asking it for a page it lacks.
    expect(screen.getByRole('link', { name: /dev panels/ })).toHaveAttribute(
      'href',
      'http://127.0.0.1:8789',
    )
  })

  it('still routes none of the panels itself, stamped or not', () => {
    stampDevPanels()
    renderAt('/')
    expect(screen.queryByRole('link', { name: 'retrieval' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'telemetry' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'lab' })).not.toBeInTheDocument()
  })
})
