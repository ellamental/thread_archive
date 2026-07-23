// The home landing page: a hero search box over the most recent conversations,
// grouped Today / Yesterday / Earlier, with loading, error, and empty states.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { Landing } from '../components/Landing'
import type { ThreadListItem } from '../api'
import { mswError, mswJson, mswPending } from './msw'

function PageStub() {
  const loc = useLocation()
  return <div>PAGE {loc.pathname}</div>
}

function renderLanding() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="*" element={<PageStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

function thread(overrides: Partial<ThreadListItem>): ThreadListItem {
  return {
    id: '1', title: 'a session', source: 'claude-code', thread_type: 'conversation',
    updated_at: '2026-01-01T10:00:00Z',
    ...overrides,
  }
}

// dayGroup buckets against the real clock, so anchor fixtures to calendar-day
// offsets from now rather than fixed dates.
function isoOffsetDays(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString()
}

describe('Landing', () => {
  it('shows the loading state while recents are in flight', () => {
    mswJson('/api/sources', { sources: [] }) // the hero SearchBox's own fetch
    mswPending('/api/threads')
    renderLanding()
    expect(screen.getByText('Loading recent conversations…')).toBeInTheDocument()
  })

  it('says recents are unavailable when the fetch fails', async () => {
    mswJson('/api/sources', { sources: [] })
    mswError('/api/threads', 500, 'boom')
    renderLanding()
    expect(
      await screen.findByText('Recent conversations are unavailable.'),
    ).toBeInTheDocument()
  })

  it('says so when there are no conversations yet', async () => {
    mswJson('/api/sources', { sources: [] })
    mswJson('/api/threads', { threads: [] })
    renderLanding()
    expect(await screen.findByText('No conversations yet.')).toBeInTheDocument()
  })

  it('groups recents by day and links each card to the reader', async () => {
    mswJson('/api/sources', { sources: [] })
    mswJson('/api/threads', {
      threads: [
        thread({ id: 't1', title: 'today talk', updated_at: isoOffsetDays(0) }),
        thread({ id: 't2', title: 'yesterday talk', updated_at: isoOffsetDays(1) }),
        thread({ id: 't3', title: 'older talk', updated_at: isoOffsetDays(9) }),
        // no title / source / date: falls back to Untitled, empty meta, Earlier
        thread({ id: 't4', title: '', source: null, updated_at: null }),
        // an unparseable timestamp is treated as undated (Earlier, blank date)
        thread({ id: 't5', title: 'broken date', updated_at: 'not-a-date' }),
      ],
    })
    renderLanding()

    // every bucket appears (the null- and bad-date threads land in Earlier)
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Yesterday' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Earlier' })).toBeInTheDocument()

    // an empty title falls back, and its card still routes to the reader
    const untitled = screen.getByText('Untitled conversation')
    expect(untitled.closest('a')).toHaveAttribute('href', '/archive/t4')
    expect(screen.getByText('today talk').closest('a')).toHaveAttribute('href', '/archive/t1')

    // the meta line joins source and a formatted date, dropping the empties
    expect(screen.getAllByText(/claude-code · /).length).toBeGreaterThan(0)
  })
})
