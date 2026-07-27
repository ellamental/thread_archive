import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { RetrievalReport } from '../api'
import { RetrievalView } from '../components/RetrievalView'
import { mswError, mswJson, recordRequests } from './msw'

function report(over: Partial<RetrievalReport> = {}): RetrievalReport {
  return {
    home: '/Users/test/.thread/archive',
    hours: 14 * 24,
    bucket: 'day',
    at: new Date().toISOString(),
    served: {
      hours: 14 * 24,
      bucket: 'day',
      n: 40,
      n_unknown_regime: 12,
      buckets: [
        { at: '2026-07-25', n: 20, warm: { n: 18, p50: 240, p90: 900 }, cold: { n: 2, p50: 8200, p90: 11000 } },
        { at: '2026-07-26', n: 20, warm: { n: 16, p50: 210, p90: 800 }, cold: { n: 4, p50: 9100, p90: 13000 } },
      ],
      warm: { n: 34, p50: 228, p90: 860, p99: 2415 },
      cold: { n: 6, p50: 8600, p90: 12500, p99: 13100 },
    },
    stages: {
      n: 34,
      n_unproven: 12,
      stages: [
        { stage: 'fts_ms', n: 34, p50: 178, p90: 1344 },
        { stage: 'semantic_ms', n: 34, p50: 90, p90: 293 },
      ],
    },
    restarts: { n: 39, bucket: 'day', buckets: [{ at: '2026-07-26', n: 39 }], p50_ms: 22300, total_s: 1836 },
    bench: {
      observed: [{ at: '2026-07-26T19:00:00Z', commit: 'def', p50: 228, p95: 1412, p99: 2415, n_queries: 40, tuning: false }],
    },
    ...over,
  }
}

function view() {
  return render(
    <MemoryRouter>
      <RetrievalView />
    </MemoryRouter>,
  )
}

it('leads with the number an agent actually feels, not an average', async () => {
  mswJson('/api/retrieval', report())
  view()
  // The headline is the warm median — the typical search. An average over this
  // distribution describes no search that ran.
  expect(await screen.findByText('228ms')).toBeInTheDocument()
  expect(screen.getByText('typical search')).toBeInTheDocument()
})

it('reports the cold regime beside the warm one rather than blended into it', async () => {
  mswJson('/api/retrieval', report())
  view()
  expect(await screen.findByText('8.60s')).toBeInTheDocument()
  expect(screen.getByText('first search after a restart')).toBeInTheDocument()
  // …and names the restart count, which is what decides how often anyone pays it.
  expect(screen.getByText('process starts')).toBeInTheDocument()
  expect(screen.getAllByText('39').length).toBeGreaterThan(0)
})

it('says how much of the window cannot be sorted into a regime', async () => {
  mswJson('/api/retrieval', report())
  view()
  // Silence here would read as "all 40 searches were classified", which is the
  // error that made the ledger unreadable in the first place.
  expect(
    await screen.findByText(/12 of 40 searches predate the process-age field/),
  ).toBeInTheDocument()
})

it('renders the stage table with the slowest stage first', async () => {
  mswJson('/api/retrieval', report())
  const { container } = view()
  // The stage names appear in the section's prose too, so read the table itself.
  await screen.findByText('178ms')
  const names = [...container.querySelectorAll('.stat-table tbody td:first-child code')].map(
    (n) => n.textContent,
  )
  expect(names.slice(0, 2)).toEqual(['fts_ms', 'semantic_ms'])
})

it('survives a section the server could not assemble', async () => {
  // Each ledger is read independently, so one unreadable file must not blank the
  // page — an operator view that vanishes when an input is missing is useless.
  mswJson('/api/retrieval', report({ bench: null }))
  view()
  expect(await screen.findByText('228ms')).toBeInTheDocument()
  expect(screen.getByText(/No bench runs recorded/)).toBeInTheDocument()
})

it('says the page measures speed only, and why that is not a gap to fill', async () => {
  // A latency-only page invites "so how is quality?" — and the answer is that no
  // number about this corpus can be made honestly, not that one is pending.
  mswJson('/api/retrieval', report())
  view()
  await screen.findByText('228ms')
  expect(screen.getByText(/would have to be made by searching it/i)).toBeInTheDocument()
})

it('asks for a shorter window in hours, so a sub-day view is expressible', async () => {
  const seen = recordRequests()
  mswJson('/api/retrieval', report())
  view()
  await screen.findByText('228ms')
  await userEvent.selectOptions(screen.getByLabelText(/window/), '24')
  await waitFor(() => expect(seen).toContain('/api/retrieval?hours=24'))
})

it('draws an hourly window on the operator’s clock, not the ledger’s UTC', async () => {
  // 18:00 UTC is 13:00 in CDT; a chart that labels it 18:00 puts this afternoon's
  // spike five hours from where it felt like it happened.
  const hourly = report({
    hours: 6,
    bucket: 'hour',
    served: {
      hours: 6,
      bucket: 'hour',
      n: 3,
      n_unknown_regime: 0,
      buckets: [
        { at: '2026-07-26T16', n: 0 },
        { at: '2026-07-26T17', n: 2, warm: { n: 2, p50: 190, p90: 240 } },
        { at: '2026-07-26T18', n: 1, warm: { n: 1, p50: 220, p90: 220 } },
      ],
      warm: { n: 3, p50: 200, p90: 240, p99: 240 },
      cold: { n: 0, p50: 0, p90: 0, p99: 0 },
    },
  })
  mswJson('/api/retrieval', hourly)
  const { container } = view()
  await screen.findByText(/Median served latency per hour/)
  const served = container.querySelector('.rv-chart')!
  const clock = (iso: string) => {
    const t = new Date(iso)
    return {
      hh: `${String(t.getHours()).padStart(2, '0')}:00`,
      full: `${t.getMonth() + 1}/${t.getDate()} ${String(t.getHours()).padStart(2, '0')}:00`,
    }
  }
  const labels = [...served.querySelectorAll('text')].map((n) => n.textContent)
  expect(labels).toContain(clock('2026-07-26T17:00:00Z').hh)
  // The empty bucket is a gap, not a point: two warm samples, two dots.
  expect(served.querySelectorAll('circle')).toHaveLength(2)
  // …and the count behind a point is reachable, because at this resolution a
  // median is often over a single search.
  expect([...served.querySelectorAll('circle title')].map((n) => n.textContent)).toContain(
    `${clock('2026-07-26T18:00:00Z').full} · 1 warm search · p50 220ms`,
  )
})

it('surfaces a failed fetch instead of rendering an empty page', async () => {
  mswError('/api/retrieval', 500, 'boom')
  view()
  expect(await screen.findByText(/Could not load retrieval health/)).toBeInTheDocument()
})
