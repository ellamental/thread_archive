import { expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { RetrievalReport } from '../api'
import { RetrievalView } from '../components/RetrievalView'
import { mswError, mswJson } from './msw'

function report(over: Partial<RetrievalReport> = {}): RetrievalReport {
  return {
    home: '/Users/test/.thread/archive',
    days: 14,
    at: new Date().toISOString(),
    served: {
      days: 14,
      n: 40,
      n_unknown_regime: 12,
      daily: [
        { day: '2026-07-25', n: 20, warm: { n: 18, p50: 240, p90: 900 }, cold: { n: 2, p50: 8200, p90: 11000 } },
        { day: '2026-07-26', n: 20, warm: { n: 16, p50: 210, p90: 800 }, cold: { n: 4, p50: 9100, p90: 13000 } },
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
    restarts: { n: 39, daily: [{ day: '2026-07-26', n: 39 }], p50_ms: 22300, total_s: 1836 },
    bench: {
      gold: [{ at: '2026-07-26T10:00:00Z', commit: 'abc', p50: 900, p95: 2000, p99: 3000, n_queries: 300, tuning: false }],
      observed: [{ at: '2026-07-26T19:00:00Z', commit: 'def', p50: 228, p95: 1412, p99: 2415, n_queries: 40, tuning: false }],
    },
    quality: {
      points: [
        { at: '2026-07-26T18:00:00Z', commit: 'abc', passed: true, mrr: 0.7451, ndcg: 0.6264, n: 317 },
        { at: '2026-07-26T19:00:00Z', commit: 'def', passed: true, mrr: 0.7476, ndcg: 0.6264, n: 317 },
      ],
      latest: { at: '2026-07-26T19:00:00Z', commit: 'def', passed: true, mrr: 0.7476, ndcg: 0.6264, n: 317 },
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
  mswJson('/api/retrieval', report({ bench: null, quality: null }))
  view()
  expect(await screen.findByText('228ms')).toBeInTheDocument()
  expect(screen.getByText(/No bench runs recorded/)).toBeInTheDocument()
  expect(screen.getByText('No gold runs recorded.')).toBeInTheDocument()
})

it('surfaces a failed fetch instead of rendering an empty page', async () => {
  mswError('/api/retrieval', 500, 'boom')
  view()
  expect(await screen.findByText(/Could not load retrieval health/)).toBeInTheDocument()
})
