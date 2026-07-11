// The search page: loading / error / empty states, hits grouped per thread,
// and clicking a group or hit navigates to the thread.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SearchView } from '../components/SearchView'
import type { SearchHit } from '../api'

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/search" element={<SearchView />} />
        <Route path="/archive/:id" element={<div>THREAD PAGE</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

function okJSON(data: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
    text: () => Promise.resolve(JSON.stringify(data)),
  } as Response)
}

function hit(overrides: Partial<SearchHit>): SearchHit {
  return {
    event_id: 1, thread_id: 1, thread_title: 'Thread One', content_type: 'text',
    snippet: 'a snippet', full_content: 'full', occurred_at: '2026-01-01T10:00:00Z',
    ...overrides,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('SearchView', () => {
  it('prompts for a query when none is given', () => {
    renderAt('/search')
    expect(screen.getByText('Type a query above.')).toBeInTheDocument()
  })

  it('shows the loading state while the search is in flight', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderAt('/search?q=hello')
    expect(screen.getByText('searching…')).toBeInTheDocument()
  })

  it('surfaces a search error', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, text: () => Promise.resolve('boom') } as Response)))
    renderAt('/search?q=hello')
    expect(await screen.findByText(/search error: 500: boom/)).toBeInTheDocument()
  })

  it('says when nothing matches', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON({ query: 'hello', hits: [] })))
    renderAt('/search?q=hello')
    expect(await screen.findByText('no matches')).toBeInTheDocument()
  })

  it('groups hits by thread with a per-thread hit count', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON({
      query: 'x',
      hits: [
        hit({ event_id: 1, thread_id: 1 }),
        hit({ event_id: 2, thread_id: 1, snippet: 'second snippet' }),
        hit({ event_id: 3, thread_id: 2, thread_title: 'Thread Two', snippet: 'other thread' }),
      ],
    })))
    renderAt('/search?q=x')
    expect(await screen.findByText('Thread One')).toBeInTheDocument()
    expect(screen.getByText('#1 · 2 hits')).toBeInTheDocument()
    expect(screen.getByText('Thread Two')).toBeInTheDocument()
    expect(screen.getByText('#2 · 1 hit')).toBeInTheDocument()
    // a semantic hit is badged; the lexical ones aren't
    expect(screen.queryByText('semantic')).not.toBeInTheDocument()
  })

  it('navigates to the thread when a hit is clicked', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', vi.fn(() => okJSON({
      query: 'x', hits: [hit({ event_id: 1, thread_id: 42, thread_title: 'Target' })],
    })))
    renderAt('/search?q=x')
    await user.click(await screen.findByText('a snippet'))
    expect(screen.getByText('THREAD PAGE')).toBeInTheDocument()
  })

  it('badges semantic hits', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON({
      query: 'x', hits: [hit({ _semantic: 0.87 })],
    })))
    renderAt('/search?q=x')
    expect(await screen.findByText('semantic')).toBeInTheDocument()
  })
})
