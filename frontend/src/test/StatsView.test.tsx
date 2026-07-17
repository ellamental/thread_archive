// The stats page: loading / error states, the overview tiles, the by-provider and
// by-model tables, and the honest '—' for a source that records tokens but no cost.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { StatsView } from '../components/StatsView'
import type { Stats } from '../api'
import { mswError, mswJson, mswPending } from './msw'

function renderStats() {
  return render(
    <MemoryRouter initialEntries={['/stats']}>
      <StatsView />
    </MemoryRouter>,
  )
}

function stats(overrides: Partial<Stats> = {}): Stats {
  return {
    overview: {
      conversations: 120,
      sources: 3,
      models: 2,
      input_tokens: 900_000,
      output_tokens: 100_000,
      tokens: 1_000_000,
      cost: 3.69,
      cost_conversations: 21,
      first_at: '2026-01-18T07:33:57Z',
      last_at: '2026-07-17T00:27:10Z',
    },
    by_source: [
      {
        source: 'cloth', conversations: 21, with_tokens: 21, input_tokens: 800_000,
        output_tokens: 80_000, tokens: 880_000, avg_tokens: 41_904, with_cost: 21,
        cost: 3.69, avg_cost: 0.1757,
      },
      {
        source: 'claude-code', conversations: 99, with_tokens: 99, input_tokens: 100_000,
        output_tokens: 20_000, tokens: 120_000, avg_tokens: 1212, with_cost: 0,
        cost: null, avg_cost: null,
      },
    ],
    by_model: [
      { model: 'deepseek/deepseek-v4-pro', requests: 616, input_tokens: 500_000, output_tokens: 40_000, tokens: 540_000, cost: 2.4, conversations: 15 },
      { model: 'claude-opus-4-8', requests: 300, input_tokens: 300_000, output_tokens: 30_000, tokens: 330_000, cost: null, conversations: 40 },
    ],
    ...overrides,
  }
}

describe('StatsView', () => {
  it('shows the loading state while the survey is in flight', () => {
    mswPending('/api/stats')
    renderStats()
    expect(screen.getByText(/crunching the archive/)).toBeInTheDocument()
  })

  it('surfaces a fetch error', async () => {
    mswError('/api/stats', 500, 'boom')
    renderStats()
    expect(await screen.findByText(/stats unavailable: 500: boom/)).toBeInTheDocument()
  })

  it('renders the overview tiles', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('Stats')).toBeInTheDocument()
    // token tile compacts 1,000,000 → 1M; cost tile shows the dollar total
    expect(screen.getByText('1M')).toBeInTheDocument()
    // $3.69 appears in both the cost tile and cloth's provider row — assert presence, not uniqueness.
    expect(screen.getAllByText('$3.69').length).toBeGreaterThan(0)
    expect(screen.getByText(/120 conversations across 3 sources/)).toBeInTheDocument()
  })

  it('renders provider rows with cost, and — for a source with none', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('cloth')).toBeInTheDocument()
    expect(screen.getByText('claude-code')).toBeInTheDocument()
    // The cost-bearing source shows its per-session average; the subscription source
    // renders '—' rather than a fabricated 0.
    expect(screen.getByText('$0.1757')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('lists models with their request counts', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('deepseek/deepseek-v4-pro')).toBeInTheDocument()
    expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument()
  })

  it('links each model to its drill-down page, slash-safe', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    const link = await screen.findByRole('link', { name: 'deepseek/deepseek-v4-pro' })
    expect(link).toHaveAttribute('href', '/stats/model/deepseek%2Fdeepseek-v4-pro')
    expect(screen.getByRole('link', { name: 'claude-opus-4-8' })).toHaveAttribute(
      'href',
      '/stats/model/claude-opus-4-8',
    )
  })
})
