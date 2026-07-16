// The thread reader: loading / error / empty states, uuid → thread redirect
// via /api/archive-link, the per-thread model header, and same-model runs
// merging into one labelled group.
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThreadView } from '../components/ThreadView'
import type { Message, StructuredThread } from '../api'
import { mswError, mswJson, mswPending, recordRequests } from './msw'

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/archive/:id" element={<ThreadView />} />
      </Routes>
    </MemoryRouter>,
  )
}

function thread(messages: Message[], overrides: Partial<StructuredThread> = {}): StructuredThread {
  return { thread_id: 5, title: 'A Thread', source: 'claude-code', messages, ...overrides }
}

const asst = (text: string, model: string): Message => ({
  role: 'assistant',
  blocks: [{ type: 'text', text }],
  meta: { ts: null, models: [model] },
})

describe('ThreadView', () => {
  it('shows the loading state while the thread is in flight', () => {
    mswPending('/api/thread/:id')
    renderAt('/archive/5')
    expect(screen.getByText('loading thread 5…')).toBeInTheDocument()
  })

  it('surfaces a read error', async () => {
    mswError('/api/thread/:id', 404, 'nope')
    renderAt('/archive/5')
    expect(await screen.findByText(/read error: 404: nope/)).toBeInTheDocument()
  })

  it('resolves a pasted provider uuid to its thread instead of truncating it', async () => {
    const uuid = '27056da6-8578-4a8c-ab90-d634702dc42d'
    const requests = recordRequests()
    mswJson('/api/archive-link', { thread_id: 7, url: '/archive/7' })
    mswJson('/api/thread/:id', thread([asst('resolved!', 'opus')], { thread_id: 7 }))
    renderAt(`/archive/${uuid}`)
    expect(await screen.findByText('resolved!')).toBeInTheDocument()
    expect(requests[0]).toBe(`/api/archive-link?id=${encodeURIComponent(uuid)}`)
    expect(requests[1]).toMatch(/^\/api\/thread\/7\?/)
  })

  it('reports a dead provider link', async () => {
    mswError('/api/archive-link', 404, 'unknown id')
    renderAt('/archive/not-a-real-uuid')
    expect(await screen.findByText(/read error: 404: unknown id/)).toBeInTheDocument()
  })

  it('shows an empty state for a thread with no renderable content', async () => {
    mswJson('/api/thread/:id', thread([]))
    renderAt('/archive/5')
    expect(await screen.findByText('(no renderable content)')).toBeInTheDocument()
  })

  it('lists each model once in the header, first-seen order', async () => {
    mswJson('/api/thread/:id', thread([asst('one', 'opus'), asst('two', 'opus'), asst('three', 'haiku')]))
    renderAt('/archive/5')
    await screen.findByText('A Thread')
    // 'opus' appears as a header tag exactly once despite two opus messages
    expect(screen.getAllByText('opus')).toHaveLength(1)
    expect(screen.getAllByText('haiku')).toHaveLength(1)
  })

  it('merges a same-model run into one labelled group and re-labels on model switch', async () => {
    mswJson('/api/thread/:id', thread([
      asst('first inference', 'opus'),
      asst('tool loop continues', 'opus'),
      asst('fallback answer', 'haiku'),
    ]))
    renderAt('/archive/5')
    await screen.findByText('first inference')
    // two labelled groups: the opus run (labelled once) and the haiku switch
    expect(screen.getAllByText('assistant')).toHaveLength(2)
  })

  it('requests the thread with the thinking/tools flags of the toggles', async () => {
    const requests = recordRequests()
    mswJson('/api/thread/:id', thread([asst('hi', 'opus')]))
    renderAt('/archive/5')
    await screen.findByText('hi')
    expect(requests[0]).toBe('/api/thread/5?thinking=0&tools=1')
  })

  it('highlights and scrolls to the message a search hit deep-linked (?e=)', async () => {
    const scrolled = vi.fn()
    window.HTMLElement.prototype.scrollIntoView = scrolled
    mswJson('/api/thread/:id', thread([
      { ...asst('earlier turn', 'opus'), event_ids: [11] },
      { ...asst('the hit turn', 'opus'), event_ids: [12] },
    ]))
    renderAt('/archive/5?e=12')
    const msg = (await screen.findByText('the hit turn')).closest('.msg')
    expect(msg).toHaveClass('hit-target')
    expect(msg).toHaveAttribute('id', 'm-1')
    expect(scrolled).toHaveBeenCalled()
    // the non-target message is untouched
    expect(screen.getByText('earlier turn').closest('.msg')).not.toHaveClass('hit-target')
  })

  it('falls back to the nearest preceding message when the hit event is hidden', async () => {
    // e.g. a hit on thinking content while the thinking toggle is off: the exact
    // event renders no block, so the reader lands on the closest visible turn.
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
    mswJson('/api/thread/:id', thread([
      { ...asst('first', 'opus'), event_ids: [11] },
      { ...asst('second', 'opus'), event_ids: [14] },
    ]))
    renderAt('/archive/5?e=15')
    const msg = (await screen.findByText('second')).closest('.msg')
    expect(msg).toHaveClass('hit-target')
  })

  it('shows the provenance line: date span, event count, session id', async () => {
    mswJson('/api/thread/:id', thread([asst('hi', 'opus')], {
      source_id: '27056da6-8578-4a8c-ab90-d634702dc42d',
      started_at: '2026-01-01T10:00:00Z',
      ended_at: '2026-01-01T11:23:00Z',
      event_count: 1234,
    }))
    renderAt('/archive/5')
    await screen.findByText('hi')
    expect(screen.getByText('1,234 events')).toBeInTheDocument()
    expect(screen.getByText('27056da6-8578-4a8c-ab90-d634702dc42d')).toBeInTheDocument()
    // start → end rendered as one span
    expect(screen.getByText(/→/)).toBeInTheDocument()
  })

  it('copies a per-message permalink built on the message’s first event', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    mswJson('/api/thread/:id', thread([{ ...asst('linked turn', 'opus'), event_ids: [11, 12] }]))
    renderAt('/archive/5')
    await screen.findByText('linked turn')
    fireEvent.click(screen.getByRole('button', { name: 'copy link to this message' }))
    expect(await screen.findByText('✓ copied')).toBeInTheDocument()
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('/archive/5?e=11'))
  })
})
