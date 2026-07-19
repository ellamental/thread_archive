// The search page: loading / error / empty states, hits grouped per thread,
// clicking a group or hit navigates to the thread, plus the empty-query browse
// view and the quality / subjects orientation the results line carries.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { SearchView } from '../components/SearchView'
import type { SearchHit } from '../api'
import { mswError, mswJson, mswPending, recordRequests } from './msw'

// Stub pages that echo where they were opened, so click tests can assert the
// deep-link (?e=<event_id>, /topic/:id) and not just that navigation happened.
function ThreadStub() {
  const loc = useLocation()
  return <div>THREAD PAGE {loc.pathname + loc.search}</div>
}

function TopicStub() {
  const loc = useLocation()
  return <div>TOPIC PAGE {loc.pathname}</div>
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/search" element={<SearchView />} />
        <Route path="/archive/:id" element={<ThreadStub />} />
        <Route path="/topic/:id" element={<TopicStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

function hit(overrides: Partial<SearchHit>): SearchHit {
  return {
    event_id: 1, thread_id: '1', thread_title: 'Thread One', content_type: 'text',
    snippet: 'a snippet', full_content: 'full', occurred_at: '2026-01-01T10:00:00Z',
    ...overrides,
  }
}

describe('SearchView', () => {
  it('browses recent threads when no query is given', async () => {
    mswJson('/api/search', {
      query: '', browse: true, quality: null, subjects: [],
      hits: [
        hit({ thread_id: '5', event_id: 99, thread_title: 'Latest Session',
              thread_source: 'demo-harness', n_events: 12 }),
        hit({ thread_id: '3', event_id: 42, thread_title: 'Older Session',
              thread_source: 'codex', n_events: 1 }),
      ],
    })
    renderAt('/search')
    expect(await screen.findByText('Latest Session')).toBeInTheDocument()
    expect(screen.getByText(/recent threads — newest activity first/)).toBeInTheDocument()
    expect(screen.getByText('demo-harness')).toBeInTheDocument()
    expect(screen.getByText('12 events')).toBeInTheDocument()
    expect(screen.getByText('1 event')).toBeInTheDocument()
  })

  it('opens a browse row at the thread tail', async () => {
    const user = userEvent.setup()
    mswJson('/api/search', {
      query: '', browse: true, quality: null, subjects: [],
      hits: [hit({ thread_id: '5', event_id: 99, thread_title: 'Latest Session' })],
    })
    renderAt('/search')
    await user.click(await screen.findByText('Latest Session'))
    expect(screen.getByText('THREAD PAGE /archive/5?e=99')).toBeInTheDocument()
  })

  it('says when the browse window is empty', async () => {
    mswJson('/api/search', { query: '', browse: true, quality: null, subjects: [], hits: [] })
    renderAt('/search?source=demo-harness')
    expect(await screen.findByText('no threads in this window')).toBeInTheDocument()
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
        hit({ event_id: 1, thread_id: '1' }),
        hit({ event_id: 2, thread_id: '1', snippet: 'second snippet' }),
        hit({ event_id: 3, thread_id: '2', thread_title: 'Thread Two', snippet: 'other thread' }),
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
      query: 'x', hits: [hit({ event_id: 9, thread_id: '42', thread_title: 'Target' })],
    })
    renderAt('/search?q=x')
    await user.click(await screen.findByText('a snippet'))
    expect(screen.getByText('THREAD PAGE /archive/42?e=9')).toBeInTheDocument()
  })

  it('folds threads carrying the same text behind an expander, and opens them', async () => {
    const user = userEvent.setup()
    mswJson('/api/search', {
      query: 'x',
      hits: [hit({
        dup_threads: [
          { thread_id: '77', title: 'Fork One' },
          { thread_id: '78', title: null },
        ],
      })],
    })
    renderAt('/search?q=x')
    const toggle = await screen.findByRole('button', { name: /same text in 2 other threads/ })
    // Collapsed by default: the fold is noise removal, not a second result list.
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Fork One')).not.toBeInTheDocument()

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    // A folded thread with no title still has to be reachable.
    expect(screen.getByText('thread 78')).toBeInTheDocument()
    await user.click(screen.getByText('Fork One'))
    expect(screen.getByText('THREAD PAGE /archive/77')).toBeInTheDocument()
  })

  it('says "1 other thread" when a single thread folded', async () => {
    mswJson('/api/search', {
      query: 'x', hits: [hit({ dup_threads: [{ thread_id: '77', title: 'Fork One' }] })],
    })
    renderAt('/search?q=x')
    expect(await screen.findByRole('button', { name: /same text in 1 other thread$/ })).toBeInTheDocument()
  })

  it('shows no fold affordance on a hit nothing duplicates', async () => {
    mswJson('/api/search', { query: 'x', hits: [hit({})] })
    renderAt('/search?q=x')
    await screen.findByText('a snippet')
    expect(screen.queryByRole('button', { name: /same text in/ })).not.toBeInTheDocument()
  })

  it('badges semantic hits', async () => {
    mswJson('/api/search', { query: 'x', hits: [hit({ _semantic: 0.87 })] })
    renderAt('/search?q=x')
    expect(await screen.findByText('semantic')).toBeInTheDocument()
  })

  it('shows the quality verdict, its caution note, and per-hit K/N badges', async () => {
    mswJson('/api/search', {
      query: 'x',
      quality: { verdict: 'partial', note: 'only some query terms matched the top hit — scan before trusting', n_terms: 3 },
      hits: [hit({ term_hits: 2 }), hit({ event_id: 2, thread_id: '2', term_hits: 0, snippet: 'other' })],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('quality: partial')).toBeInTheDocument()
    expect(screen.getByText(/scan before trusting/)).toBeInTheDocument()
    expect(screen.getByText('2/3')).toBeInTheDocument()
    // a zero-term hit is badged like a semantic guess
    expect(screen.getByText('0/3')).toHaveClass('sem')
  })

  it('lists the subjects the results cluster under, linking into the topic pages', async () => {
    const user = userEvent.setup()
    mswJson('/api/search', {
      query: 'x',
      subjects: [
        { topic_id: '7', title: 'Graph Theory', chats: 2 },
        { topic_id: '9', title: 'PageRank', chats: 1 },
      ],
      hits: [hit({})],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('subjects:')).toBeInTheDocument()
    expect(screen.getByText('PageRank (1)')).toBeInTheDocument()
    await user.click(screen.getByText('Graph Theory (2)'))
    expect(screen.getByText('TOPIC PAGE /topic/7')).toBeInTheDocument()
  })

  it('sends URL-carried filters with the search (until made day-inclusive)', async () => {
    const requests = recordRequests()
    mswJson('/api/search', { query: 'x', hits: [] })
    renderAt('/search?q=x&source=demo-harness&since=2026-01-01&until=2026-02-01')
    await screen.findByText('no matches')
    const search = requests.find((r) => r.startsWith('/api/search'))
    expect(search).toContain('source=demo-harness')
    expect(search).toContain('since=2026-01-01')
    expect(search).toContain('until=2026-02-01T23%3A59%3A59')
    // the active filters are echoed on the results line
    expect(screen.getByText(/demo-harness · from 2026-01-01 · to 2026-02-01/)).toBeInTheDocument()
  })

  it('says when only the top page of hits is shown', async () => {
    mswJson('/api/search', {
      query: 'x',
      hits: Array.from({ length: 40 }, (_, i) => hit({ event_id: i + 1, thread_id: '1' })),
    })
    renderAt('/search?q=x')
    expect(await screen.findByText(/top 40 hits shown/)).toBeInTheDocument()
  })
})
