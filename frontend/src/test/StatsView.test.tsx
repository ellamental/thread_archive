// The stats page: loading / error states, the overview tiles, the by-provider and
// by-model tables, the honest '—' for a source that records tokens but no cost, and the
// four charts — the monthly stack, the per-model panels, the session-size histogram, and
// the rhythm grid — including what each says about the data it had to leave out.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
      cache_read_tokens: 4_000_000,
      output_tokens: 100_000,
      tokens: 1_000_000,
      cost: 3.69,
      cost_conversations: 21,
      first_at: '2026-01-18T07:33:57Z',
      last_at: '2026-07-17T00:27:10Z',
    },
    by_source: [
      {
        source: 'demo-harness', conversations: 21, with_tokens: 21, input_tokens: 800_000,
        cache_read_tokens: 3_500_000, output_tokens: 80_000, tokens: 880_000,
        avg_tokens: 41_904, with_cost: 21,
        cost: 3.69, avg_cost: 0.1757,
      },
      {
        source: 'claude-code', conversations: 99, with_tokens: 99, input_tokens: 100_000,
        cache_read_tokens: 500_000, output_tokens: 20_000, tokens: 120_000,
        avg_tokens: 1212, with_cost: 0,
        cost: null, avg_cost: null,
      },
    ],
    by_model: [
      { model: 'deepseek/deepseek-v4-pro', requests: 616, input_tokens: 500_000, cache_read_tokens: 3_000_000, output_tokens: 40_000, tokens: 540_000, cost: 2.4, conversations: 15 },
      { model: 'claude-opus-4-8', requests: 300, input_tokens: 300_000, cache_read_tokens: 500_000, output_tokens: 30_000, tokens: 330_000, cost: null, conversations: 40 },
    ],
    timeline: {
      months: ['2026-01', '2026-02', '2026-03'],
      undated: 4,
      conversations: [10, 40, 70],
      tokens: [100_000, 300_000, 600_000],
      cost: [null, 1.2, 2.49],
      conversations_by_source: [
        { key: 'claude-code', values: [4, 30, 65] },
        { key: 'demo-harness', values: [6, 10, 5] },
      ],
      tokens_by_model: [
        { key: 'deepseek/deepseek-v4-pro', values: [80_000, 260_000, 200_000] },
        { key: 'claude-opus-4-8', values: [20_000, 40_000, 400_000] },
      ],
    },
    session_sizes: {
      buckets: [
        { lo: 0, hi: 1_000, count: 12 },
        { lo: 1_000, hi: 5_000, count: 30 },
        { lo: 5_000, hi: 10_000, count: 41 },
        { lo: 10_000, hi: 50_000, count: 25 },
        { lo: 50_000, hi: null, count: 3 },
      ],
      sessions: 111,
      without_tokens: 9,
      median: 7_400,
      p90: 46_000,
    },
    rhythm: {
      grid: Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (_, h) => (h === 14 ? 9 : d))),
      max: 9,
      total: 189,
    },
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
    expect(screen.getByText(/900k in · 4M cached · 100k out/)).toBeInTheDocument()
    // $3.69 appears in both the cost tile and the demo-harness provider row — assert presence, not uniqueness.
    expect(screen.getAllByText('$3.69').length).toBeGreaterThan(0)
    expect(screen.getByText(/120 conversations across 3 sources/)).toBeInTheDocument()
  })

  it('renders provider rows with cost, and — for a source with none', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    // A source is named twice on the page — once in the chart legend, once in the table.
    expect(await screen.findAllByText('demo-harness')).toHaveLength(2)
    expect(screen.getAllByText('claude-code')).toHaveLength(2)
    // The cost-bearing source shows its per-session average; the subscription source
    // renders '—' rather than a fabricated 0.
    expect(screen.getByText('$0.1757')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('lists models with their request counts', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    // Once as a panel caption, once as a table row.
    expect(await screen.findAllByText('deepseek/deepseek-v4-pro')).toHaveLength(2)
    expect(screen.getAllByText('claude-opus-4-8')).toHaveLength(2)
  })

  it('links each model to its drill-down page, slash-safe', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    // A model is linked twice — from its panel above and from the table below — so both
    // routes into the drill-down are asserted rather than one being assumed unique.
    const links = await screen.findAllByRole('link', { name: 'deepseek/deepseek-v4-pro' })
    expect(links).toHaveLength(2)
    for (const link of links) expect(link).toHaveAttribute('href', '/stats/model/deepseek%2Fdeepseek-v4-pro')
    for (const link of screen.getAllByRole('link', { name: 'claude-opus-4-8' })) {
      expect(link).toHaveAttribute('href', '/stats/model/claude-opus-4-8')
    }
  })

  it('charts activity twice — the volume and the mix', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('Conversations per month')).toBeInTheDocument()
    expect(screen.getByText('Share of each month')).toBeInTheDocument()
    // Both charts read the same series, so one legend serves them and the label appears once.
    expect(screen.getAllByRole('img', { name: /conversations per month/i })).toHaveLength(2)
  })

  it('says how many conversations the timeline could not date', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText(/4 conversations carry no events/)).toBeInTheDocument()
  })

  it('gives each model its own panel, labelled with its busiest month', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('Model roster over time')).toBeInTheDocument()
    // deepseek peaks in Feb at 260k, opus in Mar at 400k — on a scale they share.
    expect(screen.getByText('peak 260k · Feb 2026')).toBeInTheDocument()
    expect(screen.getByText('peak 400k · Mar 2026')).toBeInTheDocument()
  })

  it('bins session sizes and marks the median, excluding sessions with no usage', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('How big a session gets')).toBeInTheDocument()
    expect(screen.getByText('<1k')).toBeInTheDocument()
    expect(screen.getByText('5k–10k')).toBeInTheDocument()
    expect(screen.getByText('50k+')).toBeInTheDocument()
    expect(screen.getByText('median 7k')).toBeInTheDocument()
    expect(screen.getByText(/9 more sessions logged no usage at all/)).toBeInTheDocument()
  })

  it('renders the rhythm grid as a table carrying every count', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    expect(await screen.findByText('When the work happens')).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'Mon' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'Sun' })).toBeInTheDocument()
    // The peak hour is shaded, but its count is also in the cell as text.
    expect(screen.getAllByTitle(/^Mon 14:00 — 9 sessions$/)).toHaveLength(1)
  })

  it('collapses a long table to its head and puts the tail back on request', async () => {
    const many = stats({
      by_model: Array.from({ length: 14 }, (_, i) => ({
        model: `m${String(i).padStart(2, '0')}`,
        requests: 100 - i,
        input_tokens: 1000,
        cache_read_tokens: 0,
        output_tokens: 100,
        tokens: 1100,
        cost: null,
        conversations: 1,
      })),
    })
    mswJson('/api/stats', many)
    const user = userEvent.setup()
    renderStats()

    const toggle = await screen.findByRole('button', { name: 'Show 4 more models' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('m09')).toBeInTheDocument()
    expect(screen.queryByText('m10')).not.toBeInTheDocument()

    await user.click(toggle)
    expect(screen.getByText('m13')).toBeInTheDocument()
    const collapse = screen.getByRole('button', { name: 'Show fewer' })
    expect(collapse).toHaveAttribute('aria-expanded', 'true')

    await user.click(collapse)
    expect(screen.queryByText('m10')).not.toBeInTheDocument()
  })

  it('leaves a short table alone — no toggle to expand nothing', async () => {
    mswJson('/api/stats', stats())
    renderStats()
    // Two providers and two models, both well under the fold.
    expect(await screen.findByText('Stats')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Show \d+ more/ })).not.toBeInTheDocument()
  })

  it('keeps a collapsed table scaled and colored by every row, not the visible ones', async () => {
    // The busiest model is hidden behind the fold; the bars of the rows on show must
    // still be drawn against its request count, not against the largest one visible.
    const rows = Array.from({ length: 12 }, (_, i) => ({
      model: `m${String(i).padStart(2, '0')}`,
      requests: i === 11 ? 1000 : 10,
      input_tokens: 100,
      cache_read_tokens: 0,
      output_tokens: 10,
      tokens: 110,
      cost: null,
      conversations: 1,
    }))
    mswJson('/api/stats', stats({ by_model: rows }))
    const user = userEvent.setup()
    const { container } = renderStats()

    await screen.findByText('m00')
    // .stat-table[1] is the model table; [0] is the provider table above it.
    const firstBar = () =>
      (container.querySelectorAll('.stat-table')[1].querySelector('.stat-bar-fill') as HTMLElement).style.width
    const collapsed = firstBar()
    await user.click(screen.getByRole('button', { name: 'Show 2 more models' }))
    expect(firstBar()).toBe(collapsed)
    expect(collapsed).toBe('2%') // 10 of the hidden row's 1000 requests, floored so it shows
  })

  it('drops the charts rather than drawing an empty axis when there is no history', async () => {
    const bare = stats()
    mswJson('/api/stats', {
      ...bare,
      timeline: { ...bare.timeline, months: [], conversations_by_source: [], tokens_by_model: [] },
      session_sizes: { ...bare.session_sizes, sessions: 0 },
      rhythm: { grid: Array.from({ length: 7 }, () => Array(24).fill(0)), max: 0, total: 0 },
    })
    renderStats()
    expect(await screen.findByText('Stats')).toBeInTheDocument()
    expect(screen.queryByText('Conversations per month')).not.toBeInTheDocument()
    expect(screen.queryByText('How big a session gets')).not.toBeInTheDocument()
    expect(screen.queryByText('When the work happens')).not.toBeInTheDocument()
  })
})
