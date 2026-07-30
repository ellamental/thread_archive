import { expect, it } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import type { BenchRuns } from '../api'
import { BenchRunView } from '../components/BenchRunView'
import { mswError, mswJson } from './msw'
import { ledger, queries, run } from './SearchLabView.test'

// One recorded benchmark run. The lab's summary shows a row's headline metrics
// because that is what fits in a line; these pin the rest — what the numbers
// were measured against, what moved since the last different configuration, and
// the invocation that produced them.

function view(id: string, body: BenchRuns = ledger(), detail = queries()) {
  mswJson('/api/search-lab/runs', body)
  mswJson('/api/search-lab/runs/:id/queries', detail)
  return render(
    <MemoryRouter initialEntries={[`/lab/run/${id}`]}>
      <Routes>
        <Route path="/lab/run/:id" element={<BenchRunView />} />
      </Routes>
    </MemoryRouter>,
  )
}

function section(name: string): HTMLElement {
  return screen.getByRole('heading', { name }).closest('section') as HTMLElement
}

it('shows every number the run recorded, not the row headline set', async () => {
  view('aaaa11112222')

  const measured = within(await screenSection('What it measured'))
  // The declared metrics are what the row is judged on; the ones beside them are
  // how a movement in those gets explained, and the summary has no room for them.
  expect(measured.getByText('ndcg10')).toBeInTheDocument()
  expect(measured.getByText('recall100')).toBeInTheDocument()
  expect(measured.getByText(/not a headline metric/)).toBeInTheDocument()
})

it('reads a delta against the last different configuration', async () => {
  view('aaaa11112222')

  const measured = within(await screenSection('What it measured'))
  // 0.709 against the 0.681 of the earlier pass under different code.
  expect(measured.getByText('+0.028')).toBeInTheDocument()
  expect(measured.getByText(/same measurement twice/)).toBeInTheDocument()
})

it('does not invent a movement for a row measured only once', async () => {
  view('aaaa11112222', ledger({ runs: [run()] }))

  const measured = within(await screenSection('What it measured'))
  expect(measured.getByText(/nothing behind it to read a movement against/)).toBeInTheDocument()
  expect(measured.queryByText('vs last config')).toBeNull()
})

it('names the pair that decides whether the numbers may still be reported', async () => {
  view('aaaa11112222')

  const conditions = within(await screenSection('Conditions'))
  expect(conditions.getByText('d46b8e7f00000000')).toBeInTheDocument()
  expect(conditions.getByText('9b7bce63')).toBeInTheDocument()
  // The commit is the label, not the identity: uncommitted edits count toward
  // the code hash, so several configurations share one commit.
  expect(conditions.getByText(/human label only/)).toBeInTheDocument()
})

it('says a corpus with no cheap identity has none, rather than blanking', async () => {
  view('aaaa11112222', ledger({ runs: [run({ corpus_id: null })] }))

  const conditions = within(await screenSection('Conditions'))
  expect(conditions.getByText(/one small home per question/)).toBeInTheDocument()
})

it('shows the whole invocation', async () => {
  view('aaaa11112222')

  // A row runs in its own process, so this line is the entire cause of the
  // numbers above it and must never be elided to fit.
  const invocation = within(await screenSection('Invocation'))
  expect(
    invocation.getByText('search_lab/beir_eval.py --dataset scifact --vectors'),
  ).toBeInTheDocument()
})

it('shows what the run cost, not only what it scored', async () => {
  view('aaaa11112222')

  const section = await screenSection('Performance')
  // A configuration is a pair of results. Recording only the score files a pass
  // that lifted nDCG and doubled the tail as a win.
  const tiles = within(section.querySelector('.stat-tiles') as HTMLElement)
  expect(tiles.getByText('171 ms')).toBeInTheDocument() // p50
  expect(tiles.getByText('377 ms')).toBeInTheDocument() // p99
  expect(tiles.getByText('5.45/s')).toBeInTheDocument()
})

it('separates the scored loop from what the row spent getting ready', async () => {
  view('aaaa11112222')

  // 59.3s of process against 55s of scoring: the rest is ingest, embed, model
  // load. Reporting only the total reads as the queries having been slow.
  const perf = within(await screenSection('Performance'))
  expect(perf.getByText(/getting ready/)).toBeInTheDocument()
})

it('breaks the median query down by stage, biggest first', async () => {
  view('aaaa11112222')

  const section = await screenSection('Performance')
  // The stage table, not the prose above it that names the two pool arms.
  const table = within(
    within(section).getByRole('columnheader', { name: 'share of p50' })
      .closest('table') as HTMLElement,
  )
  expect(table.getByText('rank_ms')).toBeInTheDocument()
  expect(table.getByText('fts_ms')).toBeInTheDocument()
  // The stage eating most of the query leads — that is the whole use of the
  // breakdown, and alphabetical order would bury it.
  const stages = table.getAllByText(/_ms$/).map((el) => el.textContent)
  expect(stages[0]).toBe('rank_ms')
  // And the arms are named, because which ran conditions every number above.
  expect(within(section).getByText('lexical + vectors')).toBeInTheDocument()
})

it('reads the cost against the previous configuration too', async () => {
  view('aaaa11112222')

  const perf = within(await screenSection('Performance'))
  // Not just the score moved: p50 210.4 → 170.7 is the other half of the trade,
  // and it is only legible with the earlier run's profile beside it.
  expect(perf.getByText('was 210 ms')).toBeInTheDocument()
  expect(perf.getByText('last config')).toBeInTheDocument()
})

it('says a run kept no cost profile rather than showing zeros', async () => {
  view('aaaa11112222', ledger({ runs: [run({ performance: null })] }))

  // An absent profile and a run that was instant are different facts, and a
  // table of zeros would make the first unreadable as anything but the second.
  const perf = within(await screenSection('Performance'))
  expect(perf.getByText(/recorded no cost profile/)).toBeInTheDocument()
})

it('does not claim a breakdown for searches that did no retrieval', async () => {
  view(
    'aaaa11112222',
    ledger({ runs: [run({ performance: { ...run().performance!, staged: 120 } })] }),
  )

  // A pool-cache hit sits both arms out. Averaging those in as instant searches
  // would drag every stage median toward zero.
  const perf = within(await screenSection('Performance'))
  expect(perf.getByText(/120 of 300/)).toBeInTheDocument()
  expect(perf.getByText(/served from the pool cache/)).toBeInTheDocument()
})

it('lists the queries the run actually failed', async () => {
  view('aaaa11112222')

  const per = within(await screenSection('Per query'))
  // The aggregate says the row scores what it scores; only this says which
  // queries it got wrong, and what it returned instead of the answer.
  expect(per.getByText(/what did we decide about the retry budget/)).toBeInTheDocument()
  expect(per.getByText(/never retrieved their gold document at all/)).toBeInTheDocument()
})

it('separates a gold document ranked too deep from one never retrieved', async () => {
  view('aaaa11112222')

  // Both score 0.0 at k=10 and they are different bugs: one is a ranking
  // problem, the other a recall problem, and they are fixed in different places.
  const per = within(await screenSection('Per query'))
  const miss = per.getByText(/retry budget/).closest('tr') as HTMLElement
  const deep = per.getByText(/pool cache get invalidated/).closest('tr') as HTMLElement
  expect(within(miss).getByText('not found')).toBeInTheDocument()
  expect(within(deep).getByText('40')).toBeInTheDocument()
  expect(within(deep).queryByText('not found')).toBeNull()
})

it('offers the comparison only when both runs still have their detail', async () => {
  view('aaaa11112222')

  // The store is capped, so the join has two ways to be unavailable — and a
  // control that cannot answer is worse than no control.
  const per = within(await screenSection('Per query'))
  expect(per.getByRole('button', { name: /what moved/ })).toBeInTheDocument()
})

it('does not offer a comparison against a run whose detail was pruned', async () => {
  view(
    'aaaa11112222',
    ledger({
      runs: [run(), run({ id: 'bbbb33334444', code_id: 'other', current: false,
                          has_queries: false })],
    }),
  )

  const per = within(await screenSection('Per query'))
  expect(per.queryByRole('button', { name: /what moved/ })).toBeNull()
})

it('lists what moved between two configurations, biggest regression first', async () => {
  const { default: userEvent } = await import('@testing-library/user-event')
  view('aaaa11112222')

  const per = within(await screenSection('Per query'))
  // Registered after the default so it wins: the same route, answered as the
  // comparison rather than the worst-first read.
  mswJson('/api/search-lab/runs/:id/queries', queries({
    order: 'moved',
    compared_to: 'bbbb33334444',
    rows: [
      {
        qid: 'lost', query: 'the query that stopped working', latency_ms: 200,
        rank: null, n_gold: 1, found: 0, measures: { ndcg10: 0 },
        before: { measures: { ndcg10: 0.85 }, rank: 2, latency_ms: 190 },
        moved: -0.85,
      },
    ],
  }))
  await userEvent.click(per.getByRole('button', { name: /what moved/ }))

  // Two configurations differing by 0.004 in the mean have moved a lot on a
  // few queries; which few is the entire content of the change.
  expect(await per.findByText('the query that stopped working')).toBeInTheDocument()
  expect(per.getByText('-0.850')).toBeInTheDocument()
  expect(per.getByText('was 2')).toBeInTheDocument()
})

it('says a run whose detail was pruned has none, rather than showing empty', async () => {
  view('aaaa11112222', ledger({ runs: [run({ has_queries: false })] }))

  const per = within(await screenSection('Per query'))
  expect(per.getByText(/store is capped/)).toBeInTheDocument()
})

it('reports a failed run as one, and explains the absence of numbers', async () => {
  view('cccc55556666')

  // Scoped to the header: the row's history below names its failures too, and
  // this is about what *this* run is.
  const heading = await screen.findByRole('heading', { level: 1 })
  const head = within(heading.closest('header') as HTMLElement)
  expect(head.getByText('failed')).toBeInTheDocument()
  expect(head.getByText(/off the bench/)).toBeInTheDocument()
  expect(
    within(section('What it measured')).getByText(/what a failure has to report/),
  ).toBeInTheDocument()
})

it("lists the row's other runs and marks the one being read", async () => {
  view('bbbb33334444')

  const history = within(await screenSection("This row's history"))
  const rows = history.getAllByRole('row').slice(1) // drop the header
  expect(rows).toHaveLength(2)
  expect(rows.filter((r) => r.className.includes('active'))).toHaveLength(1)
})

it('says plainly when the ledger has no such run', async () => {
  view('nosuchrun0000')

  expect(await screen.findByText(/No run/)).toBeInTheDocument()
})

it('points at truncation when a link could not reach that far back', async () => {
  view('nosuchrun0000', ledger({ total: 900, returned: 3 }))

  // A dead link because the ledger was read to a bound is a different fact from
  // a dead link because the record is gone, and only one of them is fixable.
  expect(await screen.findByText(/newest 3 of 900/)).toBeInTheDocument()
})

it('explains itself when the ledger cannot be read', async () => {
  mswError('/api/search-lab/runs', 404, 'the benchmark ledger ships with the search lab')
  render(
    <MemoryRouter initialEntries={['/lab/run/aaaa11112222']}>
      <Routes>
        <Route path="/lab/run/:id" element={<BenchRunView />} />
      </Routes>
    </MemoryRouter>,
  )

  expect(await screen.findByText(/Could not load the benchmark ledger/)).toBeInTheDocument()
})

/**
 * A section, once the fetch behind it has landed. The heading renders off the
 * ledger; a section with its own request (per-query detail) is still loading at
 * that point, so wait the placeholder out rather than reading a half-built one.
 */
async function screenSection(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name })
  const section = heading.closest('section') as HTMLElement
  await waitFor(() => expect(within(section).queryByText('Loading…')).toBeNull())
  return section
}
