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
      // The pooled warm median sits between the two workloads on purpose: the
      // headline must come from the interactive band, never the pool.
      warm_interactive: { n: 28, p50: 205, p90: 640, p99: 1900 },
      warm_bulk: { n: 6, p50: 4100, p90: 9800, p99: 11200 },
      cold: { n: 6, p50: 8600, p90: 12500, p99: 13100 },
      // Both shapes of door, because the page's job is to keep them apart: a
      // warmed server whose cold share is small, and a one-shot surface whose
      // cold share is everything.
      by_surface: [
        { surface: 'mcp-http', n: 36, n_cold: 2, p50: 240, p90: 900 },
        { surface: 'cli', n: 4, n_cold: 4, p50: 8600, p90: 12500 },
      ],
    },
    stages: {
      n: 34,
      n_unproven: 12,
      stages: [
        { stage: 'fts_ms', n: 34, p50: 178, p90: 1344 },
        { stage: 'semantic_ms', n: 34, p50: 90, p90: 293 },
      ],
    },
    restarts: {
      n: 39, bucket: 'day', buckets: [{ at: '2026-07-26', n: 39 }],
      by_surface: [{ surface: 'mcp-http', n: 21 }, { surface: 'web', n: 18 }],
      p50_ms: 22300, total_s: 1836,
    },
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
  // The headline is the warm *interactive* median — a first-page question. The
  // pooled warm median (228ms here) carries the bulk sweeps and describes the
  // window's workload mix, so it must not be the tile.
  expect(await screen.findByText('205ms')).toBeInTheDocument()
  expect(screen.getByText('typical search')).toBeInTheDocument()
  // Bulk work is real traffic and gets its own tile rather than a share of the
  // headline.
  expect(screen.getByText('bulk & paged')).toBeInTheDocument()
  expect(screen.getByText('4.10s')).toBeInTheDocument()
})

it('reports the cold regime beside the warm one rather than blended into it', async () => {
  mswJson('/api/retrieval', report())
  const { container } = view()
  // Scoped to the tiles: the cold median is also the CLI row's median in the
  // surface table below, and realistically so — that surface is the cold traffic.
  await screen.findByText('first search after a restart')
  expect(container.querySelector('.stat-tiles')!.textContent).toContain('8.60s')
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
    await screen.findByText(/12 of 40 searches predate the stage probe/),
  ).toBeInTheDocument()
})

it('keeps the never-warmed surfaces off the shared server’s account', async () => {
  mswJson('/api/retrieval', report())
  view()
  // The whole misreading this section prevents: a one-shot surface is cold on
  // every call it will ever serve, so pooled it reads as a warming failure in a
  // server that is warming correctly. Both rows, with their own cold shares.
  expect(await screen.findByText('mcp-http')).toBeInTheDocument()
  expect(screen.getByText('cli')).toBeInTheDocument()
  expect(screen.getByText('(6%)')).toBeInTheDocument()
  expect(screen.getByText('(100%)')).toBeInTheDocument()
})

it('attributes restarts to the daemon that paid them', async () => {
  mswJson('/api/retrieval', report())
  view()
  // 39 starts across two services is not 39 bounces of the one being read.
  expect(await screen.findByText(/mcp-http 21 · web 18/)).toBeInTheDocument()
})

it('renders the stage table with the slowest stage first', async () => {
  mswJson('/api/retrieval', report())
  const { container } = view()
  // The stage names appear in the section's prose too, and the surface table below
  // is also a `.stat-table` of `code` names — so read this table itself.
  await screen.findByText('178ms')
  const names = [...container.querySelectorAll('.rv-stages tbody td:first-child code')].map(
    (n) => n.textContent,
  )
  expect(names.slice(0, 2)).toEqual(['fts_ms', 'semantic_ms'])
})

it('survives a section the server could not assemble', async () => {
  // Each ledger is read independently, so one unreadable file must not blank the
  // page — an operator view that vanishes when an input is missing is useless.
  mswJson('/api/retrieval', report({ bench: null }))
  view()
  expect(await screen.findByText('205ms')).toBeInTheDocument()
  expect(screen.getByText(/No bench runs recorded/)).toBeInTheDocument()
})

it('says the page measures speed only, and why that is not a gap to fill', async () => {
  // A latency-only page invites "so how is quality?" — and the answer is that no
  // number about this corpus can be made honestly, not that one is pending.
  mswJson('/api/retrieval', report())
  view()
  await screen.findByText('205ms')
  expect(screen.getByText(/would have to be made by searching it/i)).toBeInTheDocument()
})

it('asks for a shorter window in hours, so a sub-day view is expressible', async () => {
  const seen = recordRequests()
  mswJson('/api/retrieval', report())
  view()
  await screen.findByText('205ms')
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
      warm_interactive: { n: 3, p50: 200, p90: 240, p99: 240 },
      warm_bulk: { n: 0, p50: 0, p90: 0, p99: 0 },
      cold: { n: 0, p50: 0, p90: 0, p99: 0 },
      by_surface: [{ surface: 'mcp-http', n: 3, n_cold: 0, p50: 200, p90: 240 }],
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
