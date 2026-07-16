// The search page: loading / error / empty states, hits grouped per thread,
// and clicking a group or hit navigates to the thread.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { SearchView } from '../components/SearchView'
import type { SearchHit } from '../api'
import { mswError, mswJson, mswPending, recordRequests } from './msw'

// Stub thread page that echoes where it was opened, so hit-click tests can
// assert the deep-link (?e=<event_id>) and not just that navigation happened.
function ThreadStub() {
  const loc = useLocation()
  return <div>THREAD PAGE {loc.pathname + loc.search}</div>
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/search" element={<SearchView />} />
        <Route path="/archive/:id" element={<ThreadStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

function hit(overrides: Partial<SearchHit>): SearchHit {
  return {
    event_id: 1, thread_id: 1, thread_title: 'Thread One', content_type: 'text',
    snippet: 'a snippet', full_content: 'full', occurred_at: '2026-01-01T10:00:00Z',
    ...overrides,
  }
}

describe('SearchView', () => {
  it('prompts for a query when none is given', () => {
    renderAt('/search')
    expect(screen.getByText('Type a query above.')).toBeInTheDocument()
  })

  it('shows the loading state while the search is in flight', () => {
    mswPending('/api/search')
    renderAt('/search?q=hello')
    expect(screen.getByText('searching…')).toBeInTheDocument()
  })

  it('surfaces a search error', async () => {
    mswError('/api/search', 500, 'boom')
    renderAt('/search?q=hello')
    expect(await screen.findByText(/search error: 500: boom/)).toBeInTheDocument()
  })

  it('says when nothing matches', async () => {
    mswJson('/api/search', { query: 'hello', hits: [] })
    renderAt('/search?q=hello')
    expect(await screen.findByText('no matches')).toBeInTheDocument()
  })

  it('groups hits by thread with a per-thread hit count', async () => {
    mswJson('/api/search', {
      query: 'x',
      hits: [
        hit({ event_id: 1, thread_id: 1 }),
        hit({ event_id: 2, thread_id: 1, snippet: 'second snippet' }),
        hit({ event_id: 3, thread_id: 2, thread_title: 'Thread Two', snippet: 'other thread' }),
      ],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('Thread One')).toBeInTheDocument()
    expect(screen.getByText('#1 · 2 hits')).toBeInTheDocument()
    expect(screen.getByText('Thread Two')).toBeInTheDocument()
    expect(screen.getByText('#2 · 1 hit')).toBeInTheDocument()
    // a semantic hit is badged; the lexical ones aren't
    expect(screen.queryByText('semantic')).not.toBeInTheDocument()
  })

  it('navigates to the thread at the matching event when a hit is clicked', async () => {
    const user = userEvent.setup()
    mswJson('/api/search', {
      query: 'x', hits: [hit({ event_id: 9, thread_id: 42, thread_title: 'Target' })],
    })
    renderAt('/search?q=x')
    await user.click(await screen.findByText('a snippet'))
    expect(screen.getByText('THREAD PAGE /archive/42?e=9')).toBeInTheDocument()
  })

  it('badges semantic hits', async () => {
    mswJson('/api/search', { query: 'x', hits: [hit({ _semantic: 0.87 })] })
    renderAt('/search?q=x')
    expect(await screen.findByText('semantic')).toBeInTheDocument()
  })

  it('sends URL-carried filters with the search (until made day-inclusive)', async () => {
    const requests = recordRequests()
    mswJson('/api/search', { query: 'x', hits: [] })
    renderAt('/search?q=x&source=cloth&since=2026-01-01&until=2026-02-01')
    await screen.findByText('no matches')
    const search = requests.find((r) => r.startsWith('/api/search'))
    expect(search).toContain('source=cloth')
    expect(search).toContain('since=2026-01-01')
    expect(search).toContain('until=2026-02-01T23%3A59%3A59')
    // the active filters are echoed on the results line
    expect(screen.getByText(/cloth · from 2026-01-01 · to 2026-02-01/)).toBeInTheDocument()
  })

  it('says when only the top page of hits is shown', async () => {
    mswJson('/api/search', {
      query: 'x',
      hits: Array.from({ length: 40 }, (_, i) => hit({ event_id: i + 1, thread_id: 1 })),
    })
    renderAt('/search?q=x')
    expect(await screen.findByText(/top 40 hits shown/)).toBeInTheDocument()
  })
})
