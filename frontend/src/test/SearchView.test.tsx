// The search page: loading / error / empty states, hits grouped per thread,
// clicking a group or hit navigates to the thread, plus the empty-query browse
// view and the quality / subjects orientation the results line carries.
import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { SearchView } from '../components/SearchView'
import { SEARCH_PAGE_SIZE, type SearchHit } from '../api'
import { http, HttpResponse, mswError, mswHandler, mswJson, mswPending, recordRequests } from './msw'

// Stub page that echoes where it was opened, so click tests can assert the
// deep-link (?e=<event_id>) and not just that navigation happened.
function ThreadStub() {
  const loc = useLocation()
  return <div>THREAD PAGE {loc.pathname + loc.search}</div>
}

function LocationStub() {
  const loc = useLocation()
  return <div data-testid="location">{loc.pathname + loc.search}</div>
}

function renderAt(url: string) {
  mswJson('/api/sources', { sources: [{ source: 'demo-harness', threads: 3 }] })
  return render(
    <MemoryRouter initialEntries={[url]}>
      <LocationStub />
      <Routes>
        <Route path="/search" element={<SearchView />} />
        <Route path="/archive/:id" element={<ThreadStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Stub /api/search as a real paginated set: it slices the fixture by the
 * request's `page`/`limit` and reports the whole set's size, the same contract
 * the server's `Results` carries. */
function stubPagedSearch(fixture: SearchHit[], extra: Record<string, unknown> = {}) {
  mswHandler(
    http.get('/api/search', ({ request }) => {
      const params = new URL(request.url).searchParams
      const page = Number(params.get('page') ?? 1)
      const size = Number(params.get('limit') ?? SEARCH_PAGE_SIZE)
      return HttpResponse.json({
        query: params.get('q') ?? '',
        browse: !params.get('q'),
        quality: null,
        subjects: [],
        hits: fixture.slice((page - 1) * size, page * size),
        total: fixture.length,
        total_threads: new Set(fixture.map((h) => h.thread_id)).size,
        capped: false,
        exhaustive: true,
        page,
        pages: Math.ceil(fixture.length / size),
        page_size: size,
        ...extra,
      })
    }),
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
    expect(screen.getByText('2 hits')).toBeInTheDocument()
    expect(screen.getByText('Thread Two')).toBeInTheDocument()
    expect(screen.getByText('1 hit')).toBeInTheDocument()
    expect(screen.queryByText('#1 · 2 hits')).not.toBeInTheDocument()
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
    expect(screen.getByText('THREAD PAGE /archive/42?e=9&q=x')).toBeInTheDocument()
  })

  it('lists every thread carrying the same text as its own result', async () => {
    // A fork or a fleet of agents on one prompt produces several threads with
    // identical text. They are several conversations, so they are several rows.
    mswJson('/api/search', {
      query: 'x',
      hits: [
        hit({ event_id: 1, thread_id: '77', thread_title: 'Fork One' }),
        hit({ event_id: 2, thread_id: '78', thread_title: 'Fork Two' }),
      ],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('Fork One')).toBeInTheDocument()
    expect(screen.getByText('Fork Two')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /same text in/ })).not.toBeInTheDocument()
  })

  it('badges semantic hits', async () => {
    mswJson('/api/search', { query: 'x', hits: [hit({ _semantic: 0.87 })] })
    renderAt('/search?q=x')
    expect(await screen.findByText('meaning match')).toBeInTheDocument()
  })

  it('shows the quality verdict, its caution note, and per-hit K/N badges', async () => {
    mswJson('/api/search', {
      query: 'x',
      quality: { verdict: 'partial', note: 'only some query terms matched the top hit — scan before trusting', n_terms: 3 },
      hits: [hit({ term_hits: 2 }), hit({ event_id: 2, thread_id: '2', term_hits: 0, snippet: 'other' })],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('mixed match')).toBeInTheDocument()
    expect(screen.getByText(/scan before trusting/)).toBeInTheDocument()
    expect(screen.getByText('2 of 3 words')).toBeInTheDocument()
    // a zero-term hit is badged like a semantic guess
    expect(screen.getByText('0 of 3 words')).toHaveClass('sem')
  })

  it('lists the subjects the results cluster under', async () => {
    mswJson('/api/search', {
      query: 'x',
      subjects: [
        { topic_id: '7', title: 'Graph Theory', chats: 2 },
        { topic_id: '9', title: 'PageRank', chats: 1 },
      ],
      hits: [hit({})],
    })
    renderAt('/search?q=x')
    expect(await screen.findByText('common subjects:')).toBeInTheDocument()
    expect(screen.getByText('PageRank (1)')).toBeInTheDocument()
    expect(screen.getByText('Graph Theory (2)')).toBeInTheDocument()
    expect(screen.getByText('PageRank (1)')).toHaveClass('subject-tag')
    expect(screen.getByText('PageRank (1)')).not.toHaveClass('chip')
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

  it('walks the ranked results and carries the page in the URL', async () => {
    const user = userEvent.setup()
    stubPagedSearch(
      Array.from({ length: SEARCH_PAGE_SIZE + 1 }, (_, i) =>
        hit({ event_id: i + 1, thread_id: String(i + 1), snippet: `snippet ${i + 1}` }),
      ),
    )
    renderAt('/search?q=x')
    expect(await screen.findByText('snippet 1')).toBeInTheDocument()
    expect(screen.getByText(/40 of 41 matches · page 1 of 2/)).toBeInTheDocument()
    expect(screen.queryByText('snippet 41')).not.toBeInTheDocument()

    await user.click(
      within(screen.getByRole('navigation', { name: 'result pages top' })).getByText('Next'),
    )
    expect(await screen.findByText('snippet 41')).toBeInTheDocument()
    expect(screen.getByText(/1 of 41 matches · page 2 of 2/)).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/search?q=x&page=2')
  })

  it('walks the browse rows too, counting threads', async () => {
    const user = userEvent.setup()
    stubPagedSearch(
      Array.from({ length: SEARCH_PAGE_SIZE + 1 }, (_, i) =>
        hit({ event_id: i + 1, thread_id: String(i + 1), thread_title: `Session ${i + 1}` }),
      ),
    )
    renderAt('/search')
    expect(await screen.findByText('Session 1')).toBeInTheDocument()
    expect(screen.getByText(/40 of 41 threads · page 1 of 2/)).toBeInTheDocument()

    await user.click(
      within(screen.getByRole('navigation', { name: 'result pages bottom' })).getByText('Last'),
    )
    expect(await screen.findByText('Session 41')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/search?page=2')
  })

  it('opens a page named in the URL', async () => {
    stubPagedSearch(
      Array.from({ length: SEARCH_PAGE_SIZE + 1 }, (_, i) =>
        hit({ event_id: i + 1, thread_id: String(i + 1), snippet: `snippet ${i + 1}` }),
      ),
    )
    renderAt('/search?q=x&page=2')
    expect(await screen.findByText('snippet 41')).toBeInTheDocument()
    expect(screen.queryByText('snippet 1')).not.toBeInTheDocument()
  })

  it('lands on the last real page when the URL names one past the end', async () => {
    stubPagedSearch([hit({ snippet: 'the only match' })])
    renderAt('/search?q=x&page=7')
    expect(await screen.findByText('the only match')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/search?q=x')
    expect(screen.getByTestId('location')).not.toHaveTextContent('page=')
  })

  it('offers no walker when one page holds the whole set', async () => {
    stubPagedSearch([hit({ snippet: 'the only match' })])
    renderAt('/search?q=x')
    await screen.findByText('the only match')
    expect(screen.queryByRole('navigation', { name: /result pages/ })).not.toBeInTheDocument()
    expect(screen.getByText(/1 of 1 match$/)).toBeInTheDocument()
  })

  it('says so when the ranked walk cannot reach every match', async () => {
    // A saturated candidate pool: the total is real, the walk stops at what the
    // ranker scored — so the count reads `≥` and the page says what to do.
    stubPagedSearch(
      Array.from({ length: SEARCH_PAGE_SIZE }, (_, i) => hit({ event_id: i + 1, thread_id: '1' })),
      { exhaustive: false, total: 900, pages: 5 },
    )
    renderAt('/search?q=x')
    await screen.findByText(/of ≥900 matches/)
    expect(screen.getByText(/narrow the query or filters to reach the rest/)).toBeInTheDocument()
  })

  it('marks a capped total as the floor it is', async () => {
    stubPagedSearch([hit({})], { capped: true, total: 10000, pages: 1 })
    renderAt('/search?q=x')
    expect(await screen.findByText(/of 10,000\+ matches/)).toBeInTheDocument()
  })

  it('asks the server for the page named in the URL', async () => {
    const requests = recordRequests()
    stubPagedSearch([hit({})])
    renderAt('/search?q=x&page=3')
    await screen.findByText('a snippet')
    expect(requests.some((r) => r.startsWith('/api/search') && r.includes('page=3'))).toBe(true)
  })
})
