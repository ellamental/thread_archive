// The thread reader: loading / error / empty states, uuid → thread redirect
// via /api/archive-link, the per-thread model header, and same-model runs
// merging into one labelled group.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ThreadView } from '../components/ThreadView'
import type { Message, StructuredThread } from '../api'

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/archive/:id" element={<ThreadView />} />
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

function thread(messages: Message[], overrides: Partial<StructuredThread> = {}): StructuredThread {
  return { thread_id: 5, title: 'A Thread', source: 'claude-code', messages, ...overrides }
}

const asst = (text: string, model: string): Message => ({
  role: 'assistant',
  blocks: [{ type: 'text', text }],
  meta: { ts: null, models: [model] },
})

afterEach(() => vi.unstubAllGlobals())

describe('ThreadView', () => {
  it('shows the loading state while the thread is in flight', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderAt('/archive/5')
    expect(screen.getByText('loading thread 5…')).toBeInTheDocument()
  })

  it('surfaces a read error', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('nope') } as Response)))
    renderAt('/archive/5')
    expect(await screen.findByText(/read error: 404: nope/)).toBeInTheDocument()
  })

  it('resolves a pasted provider uuid to its thread instead of truncating it', async () => {
    const uuid = '27056da6-8578-4a8c-ab90-d634702dc42d'
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith('/api/archive-link')) return okJSON({ thread_id: 7, url: '/archive/7' })
      return okJSON(thread([asst('resolved!', 'opus')], { thread_id: 7 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderAt(`/archive/${uuid}`)
    expect(await screen.findByText('resolved!')).toBeInTheDocument()
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/archive-link?id=${encodeURIComponent(uuid)}`)
    expect(fetchMock.mock.calls[1][0]).toMatch(/^\/api\/thread\/7\?/)
  })

  it('reports a dead provider link', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('unknown id') } as Response)))
    renderAt('/archive/not-a-real-uuid')
    expect(await screen.findByText(/read error: 404: unknown id/)).toBeInTheDocument()
  })

  it('shows an empty state for a thread with no renderable content', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON(thread([]))))
    renderAt('/archive/5')
    expect(await screen.findByText('(no renderable content)')).toBeInTheDocument()
  })

  it('lists each model once in the header, first-seen order', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON(thread([
      asst('one', 'opus'), asst('two', 'opus'), asst('three', 'haiku'),
    ]))))
    renderAt('/archive/5')
    await screen.findByText('A Thread')
    // 'opus' appears as a header tag exactly once despite two opus messages
    expect(screen.getAllByText('opus')).toHaveLength(1)
    expect(screen.getAllByText('haiku')).toHaveLength(1)
  })

  it('merges a same-model run into one labelled group and re-labels on model switch', async () => {
    vi.stubGlobal('fetch', vi.fn(() => okJSON(thread([
      asst('first inference', 'opus'),
      asst('tool loop continues', 'opus'),
      asst('fallback answer', 'haiku'),
    ]))))
    renderAt('/archive/5')
    await screen.findByText('first inference')
    // two labelled groups: the opus run (labelled once) and the haiku switch
    expect(screen.getAllByText('assistant')).toHaveLength(2)
  })

  it('requests the thread with the thinking/tools flags of the toggles', async () => {
    const fetchMock = vi.fn((_url: string) => okJSON(thread([asst('hi', 'opus')])))
    vi.stubGlobal('fetch', fetchMock)
    renderAt('/archive/5')
    await screen.findByText('hi')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/thread/5?thinking=0&tools=1')
  })
})
