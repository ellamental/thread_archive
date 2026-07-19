// The per-model drill-down: loading / error states, the overview tiles (incl. the
// per-session distribution and compactions), the monthly series with its
// compaction-only rows, and the heaviest-sessions links into the reader.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ModelStatsView } from '../components/ModelStatsView'
import type { ModelStats } from '../api'
import { mswError, mswJson, mswPending, recordRequests } from './msw'

function renderAt(model: string) {
  return render(
    <MemoryRouter initialEntries={['/stats/model/' + encodeURIComponent(model)]}>
      <Routes>
        <Route path="/stats/model/:model" element={<ModelStatsView />} />
      </Routes>
    </MemoryRouter>,
  )
}

function modelStats(overrides: Partial<ModelStats> = {}): ModelStats {
  return {
    model: 'claude-opus-4-8',
    overview: {
      conversations: 42,
      requests: 900,
      input_tokens: 1_900_000,
      output_tokens: 100_000,
      thinking_tokens: 50_000,
      tokens: 2_000_000,
      cost: null,
      cost_conversations: 0,
      compactions: 63,
      first_at: '2026-02-06 21:02:47',
      last_at: '2026-06-29 18:16:41',
    },
    per_session: {
      min_tokens: 1_000,
      max_tokens: 400_000,
      avg_tokens: 47_619,
      median_tokens: 30_000,
      avg_requests: 21.4,
    },
    by_month: [
      { month: '2026-02', sessions: 10, requests: 200, input_tokens: 470_000, output_tokens: 30_000, tokens: 500_000, avg_tokens: 50_000, cost: null, compactions: 12 },
      // A compaction-only month: a long session started earlier kept compacting.
      { month: '2026-03', sessions: 0, requests: 0, input_tokens: 0, output_tokens: 0, tokens: 0, avg_tokens: null, cost: null, compactions: 3 },
    ],
    top_sessions: [
      { thread_id: '77', title: 'the big refactor', source: 'claude-code', at: '2026-02-10 09:00:00', tokens: 400_000, requests: 120, compactions: 9 },
      { thread_id: '78', title: null, source: 'demo-harness', at: null, tokens: 100_000, requests: 30, compactions: 0 },
    ],
    ...overrides,
  }
}

describe('ModelStatsView', () => {
  it('shows the loading state while the survey is in flight', () => {
    mswPending('/api/stats/model/:model')
    renderAt('claude-opus-4-8')
    expect(screen.getByText(/surveying claude-opus-4-8…/)).toBeInTheDocument()
  })

  it('surfaces a fetch error (an unknown model 404s)', async () => {
    mswError('/api/stats/model/:model', 404, 'no data')
    renderAt('claude-opus-4-8')
    expect(await screen.findByText(/model stats unavailable: 404: no data/)).toBeInTheDocument()
  })

  it('requests the model as an encoded path tail (slashes survive)', async () => {
    const seen = recordRequests()
    mswJson('/api/stats/model/:model', modelStats({ model: 'deepseek/deepseek-v4-pro' }))
    renderAt('deepseek/deepseek-v4-pro')
    await screen.findByText('deepseek/deepseek-v4-pro')
    expect(seen).toContain('/api/stats/model/deepseek%2Fdeepseek-v4-pro')
  })

  it('renders the overview tiles, the distribution, and compactions', async () => {
    mswJson('/api/stats/model/:model', modelStats())
    renderAt('claude-opus-4-8')
    expect(await screen.findByText('claude-opus-4-8')).toBeInTheDocument()
    expect(screen.getByText('2M')).toBeInTheDocument() // total tokens tile
    expect(screen.getByText('48k avg')).toBeInTheDocument() // per-session average
    expect(screen.getByText('min 1k · median 30k · max 400k')).toBeInTheDocument()
    expect(screen.getByText('63')).toBeInTheDocument() // compactions tile
    expect(screen.getByText('~1.5 / session')).toBeInTheDocument()
    // No cost recorded → no cost tile at all, not a $0.
    expect(screen.queryByText('known cost')).not.toBeInTheDocument()
  })

  it('renders the monthly series, keeping a compaction-only month', async () => {
    mswJson('/api/stats/model/:model', modelStats())
    renderAt('claude-opus-4-8')
    expect(await screen.findByText('Feb 2026')).toBeInTheDocument()
    const mar = screen.getByText('Mar 2026').closest('tr')!
    expect(mar).toHaveTextContent('3') // its compactions still show
  })

  it('links the heaviest sessions into the reader', async () => {
    mswJson('/api/stats/model/:model', modelStats())
    renderAt('claude-opus-4-8')
    const link = await screen.findByRole('link', { name: 'the big refactor' })
    expect(link).toHaveAttribute('href', '/archive/77')
    // An untitled session still gets a usable label.
    expect(screen.getByRole('link', { name: 'thread 78' })).toHaveAttribute('href', '/archive/78')
  })
})
