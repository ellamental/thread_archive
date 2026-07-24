// The all-threads page: every thread type listed newest-first (including the
// system/subagent and topic threads the sidebar hides), a checkbox per type to
// hide it (URL-carried via ?hide=), title filtering, and per-type row links.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { AllThreadsView, ALL_THREADS_LIMIT } from '../components/AllThreadsView'
import type { ThreadListItem, ThreadTypeCount } from '../api'
import { http, HttpResponse, mswError, mswHandler, mswJson, mswPending, recordRequests } from './msw'

function PageStub() {
  const loc = useLocation()
  return <div>PAGE {loc.pathname}</div>
}

function renderThreads(url = '/threads') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/threads" element={<AllThreadsView />} />
        <Route path="*" element={<PageStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

const TYPES: ThreadTypeCount[] = [
  { thread_type: 'conversation', threads: 40 },
  { thread_type: 'system', threads: 18 },
  { thread_type: 'topic', threads: 7 },
]

function thread(overrides: Partial<ThreadListItem>): ThreadListItem {
  return {
    id: '1', title: 'a session', source: 'claude-code', thread_type: 'conversation',
    updated_at: '2026-01-01T10:00:00Z', first_user_message: null,
    ...overrides,
  }
}

/** Stub /api/threads to echo back only the fixture threads whose type is in
 * the request's `types` — the same contract the real server honors. */
function stubThreads(fixture: ThreadListItem[]) {
  mswHandler(
    http.get('/api/threads', ({ request }) => {
      const types = (new URL(request.url).searchParams.get('types') ?? '').split(',')
      return HttpResponse.json({ threads: fixture.filter((t) => types.includes(t.thread_type)) })
    }),
  )
}

describe('AllThreadsView', () => {
  it('shows the loading state while the type census is in flight', () => {
    mswPending('/api/thread-types')
    renderThreads()
    expect(screen.getByText('loading…')).toBeInTheDocument()
  })

  it('surfaces a fetch error', async () => {
    mswError('/api/thread-types', 500, 'boom')
    renderThreads()
    expect(await screen.findByText(/threads unavailable: 500: boom/)).toBeInTheDocument()
  })

  it('starts with every type checked and asks the server for all of them', async () => {
    const seen = recordRequests()
    mswJson('/api/thread-types', { types: TYPES })
    stubThreads([
      thread({ id: '1', title: 'a session' }),
      thread({ id: '2', title: '🤖 a subagent run', thread_type: 'system' }),
      thread({ id: '3', title: 'a topic', thread_type: 'topic', source: null }),
    ])
    renderThreads()
    expect(await screen.findByText('a session')).toBeInTheDocument()
    expect(screen.getByText('🤖 a subagent run')).toBeInTheDocument()
    expect(screen.getByText('a topic')).toBeInTheDocument()
    expect(screen.getByText('3 threads')).toBeInTheDocument()
    const req = seen.find((u) => u.startsWith('/api/threads'))
    expect(decodeURIComponent(req ?? '')).toContain('types=conversation,system,topic')
    // the system checkbox is annotated as the subagent bucket
    expect(screen.getByText('subagents')).toBeInTheDocument()
    for (const box of screen.getAllByRole('checkbox')) expect(box).toBeChecked()
  })

  it('unchecking a type hides its threads and writes ?hide=', async () => {
    const user = userEvent.setup()
    mswJson('/api/thread-types', { types: TYPES })
    stubThreads([
      thread({ id: '1', title: 'a session' }),
      thread({ id: '2', title: '🤖 a subagent run', thread_type: 'system' }),
    ])
    renderThreads()
    await screen.findByText('🤖 a subagent run')
    await user.click(screen.getByRole('checkbox', { name: /system/ }))
    expect(await screen.findByText('a session')).toBeInTheDocument()
    expect(screen.queryByText('🤖 a subagent run')).not.toBeInTheDocument()
  })

  it('opens straight into a filtered view via ?hide=', async () => {
    const seen = recordRequests()
    mswJson('/api/thread-types', { types: TYPES })
    stubThreads([thread({ id: '1', title: 'a session' })])
    renderThreads('/threads?hide=system,topic')
    expect(await screen.findByText('a session')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /system/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /topic/ })).not.toBeChecked()
    const req = seen.find((u) => u.startsWith('/api/threads'))
    expect(decodeURIComponent(req ?? '')).toContain('types=conversation')
    expect(decodeURIComponent(req ?? '')).not.toContain('system')
  })

  it('says so when every type is hidden, without asking the server', async () => {
    const seen = recordRequests()
    mswJson('/api/thread-types', { types: TYPES })
    renderThreads('/threads?hide=conversation,system,topic')
    expect(await screen.findByText('every type is hidden')).toBeInTheDocument()
    expect(seen.some((u) => u.startsWith('/api/threads'))).toBe(false)
  })

  it('routes every row to the reader', async () => {
    mswJson('/api/thread-types', { types: TYPES })
    stubThreads([
      thread({ id: '5', title: 'a session' }),
      thread({ id: '9', title: 'a topic', thread_type: 'topic' }),
    ])
    renderThreads()
    const session = await screen.findByText('a session')
    expect(session.closest('a')).toHaveAttribute('href', '/archive/5')
    expect(screen.getByText('a topic').closest('a')).toHaveAttribute('href', '/archive/9')
  })

  it('sends the title filter as q', async () => {
    const user = userEvent.setup()
    const seen = recordRequests()
    mswJson('/api/thread-types', { types: TYPES })
    stubThreads([thread({ id: '1', title: 'a session' })])
    renderThreads()
    await screen.findByText('a session')
    await user.type(screen.getByPlaceholderText('filter by title…'), 'needle')
    expect(seen.some((u) => u.includes('q=needle'))).toBe(true)
  })

  it('flags a full page as truncated instead of pretending it is complete', async () => {
    mswJson('/api/thread-types', { types: TYPES })
    mswJson('/api/threads', {
      threads: Array.from({ length: ALL_THREADS_LIMIT }, (_, i) => thread({ id: String(i + 1) })),
    })
    renderThreads()
    expect(await screen.findByText(/newest 500 shown/)).toBeInTheDocument()
  })
})
