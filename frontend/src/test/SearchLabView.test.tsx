import { expect, it } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { BenchRunRecord, BenchRuns, LabInventory, RunQueries } from '../api'
import { SearchLabView } from '../components/SearchLabView'
import { mswError, mswJson } from './msw'

function inventory(over: Partial<LabInventory> = {}): LabInventory {
  return {
    cache_root: '/Users/test/.cache/thread-evals',
    cache: { bytes: 26_000_000_000, files: 84_000, truncated: false },
    code_id: 'e60a586e0b84147c',
    families: {
      beir: 'public IR benchmarks',
      'agent-sessions': 'real coding-agent sessions carrying commit provenance',
    },
    benchmarks: [
      {
        name: 'beir:scifact[vectors]',
        argv: ['search_lab/beir_eval.py'],
        corpus_home: '/homes/scifact',
        corpus_id: '9b7bce63',
        corpus_built: true,
        build_hint: 'the harness builds it on first run',
        cost_min: 5,
        est_min: 1,
        fresh: false,
        state: 'stale',
        measure_keys: ['ndcg10', 'mrr10'],
        code_id: 'f13d940f',
        last: {
          at: '2026-07-27T06:45:22+00:00',
          elapsed_s: 59.3,
          commit: 'abc1234',
          code_id: 'd46b8e7f',
          measures: { ndcg10: 0.709, mrr10: 0.667 },
        },
      },
      {
        name: 'beir:nfcorpus[lexical]',
        argv: ['search_lab/beir_eval.py'],
        corpus_home: '/homes/nfcorpus',
        corpus_id: null,
        corpus_built: false,
        build_hint: 'the harness builds it on first run',
        cost_min: 2,
        est_min: 2,
        fresh: false,
        state: 'missing',
        measure_keys: ['ndcg10'],
        code_id: 'f13d940f',
        last: null,
      },
    ],
    datasets: [
      {
        name: 'scifact',
        family: 'beir',
        harness: 'search_lab/beir_eval.py --dataset scifact',
        download: { path: '/scifact', present: true, bytes: 8_000_000 },
        homes: [
          {
            label: 'corpus',
            path: '/homes/scifact',
            built: true,
            snapshot_id: '9b7bce63',
            counts: { threads: 5183, vectors: 5848 },
            embedding_space: 'local:nomic',
            created_at: '2026-07-27T03:42:18Z',
            bytes: 92_300_000,
          },
        ],
        reference: { metric: 'nDCG@10', bm25: 0.665, dense: 0.68 },
        on_bench: ['beir:scifact[lexical]'],
      },
      {
        name: 'arguana',
        family: 'beir',
        harness: 'search_lab/beir_eval.py --dataset arguana',
        download: { path: '/arguana', present: false },
        homes: [
          {
            label: 'corpus',
            path: '/homes/arguana',
            built: false,
            snapshot_id: null,
            counts: {},
            embedding_space: null,
            created_at: null,
          },
        ],
        reference: { metric: 'nDCG@10', bm25: 0.315, dense: 0.48 },
        on_bench: [],
      },
      {
        name: 'swe-chat',
        family: 'agent-sessions',
        harness: 'search_lab/swechat_corpus.py',
        download: { path: '/swe-chat-data', present: true, bytes: 1_000_000_000 },
        homes: [
          {
            label: 'corpus',
            path: '/homes/swe-chat',
            built: true,
            snapshot_id: 'c4137bd4',
            counts: { threads: 5124, vectors: 269_497 },
            embedding_space: 'local:nomic',
            created_at: '2026-07-25T07:10:34Z',
            bytes: 24_900_000_000,
          },
        ],
        reference: {},
        on_bench: [],
      },
    ],
    miners: [
      {
        name: 'commit',
        summary: 'author queries from a commit; test the session that produced it',
        measures: 'recall (provenance gold)',
        unit: 'linked session',
        cost: '1 agent / session',
        target_kind: 'per-case',
        target_help: 'commit-linked sessions to sample',
        default_target: 5,
        gold_source: 'commit provenance — no search runs during labeling',
        retrieval_free: true,
        runnable_in_all: false,
        cases_stem: 'commit-cases',
        stages: [
          { name: 'provenance', kind: 'free', summary: "session's trail edits the commit's files" },
          { name: 'alignment', kind: 'agent', summary: 'commit and session are the same work' },
          { name: 'author', kind: 'agent', summary: 'blind authoring of the queries' },
        ],
        runs: [
          {
            at: '2026-07-24T22:32:29Z',
            snapshot_id: 'c4137bd4',
            attempted: 25,
            written: 75,
            failed: 0,
            outcomes: { ok: 20, misattributed: 3, 'untargetable-commit': 2 },
            cost_usd: 9.5,
            funnel: [
              { stage: 'linkage', kind: 'free', in: 1284, out: 1149, reasons: { 'absent-from-snapshot': 135 } },
              { stage: 'sample', kind: 'free', in: 1149, out: 25, reasons: { 'not-drawn': 1124 } },
              { stage: 'provenance', kind: 'free', in: 25, out: 23, reasons: { ok: 23, 'no-file-overlap': 2 } },
              { stage: 'alignment', kind: 'agent', in: 23, out: 20, reasons: { ok: 20, misattributed: 1, 'untargetable-commit': 2 }, cost_usd: 7.1 },
              { stage: 'author', kind: 'agent', in: 20, out: 20, reasons: { ok: 20 }, cost_usd: 2.4 },
            ],
          },
        ],
        runs_total: 1,
      },
      {
        name: 'pooled',
        summary: 'grade a multi-system pool of results for a real query',
        measures: 'relevance over real traffic',
        unit: 'query',
        cost: '1 agent / query',
        target_kind: 'per-case',
        target_help: 'queries to judge',
        default_target: 5,
        gold_source: 'multi-system pooled judgment',
        retrieval_free: false,
        runnable_in_all: true,
        cases_stem: 'pooled-cases',
        stages: [],
        runs: [],
        runs_total: 0,
      },
    ],
    ...over,
  }
}

/** A ledger holding what a newest-per-row summary cannot: an older pass of a row
 *  the bench still runs, a failure, and a row the manifest no longer names. */
export function run(over: Partial<BenchRunRecord> = {}): BenchRunRecord {
  return {
    id: 'aaaa11112222',
    at: '2026-07-27T06:45:22+00:00',
    row: 'beir:scifact[vectors]',
    status: 'ok',
    code_id: 'd46b8e7f00000000',
    corpus_id: '9b7bce63',
    commit: 'abc1234',
    elapsed_s: 59.3,
    measures: { n: 300, ndcg10: 0.709, mrr10: 0.667, recall100: 0.915 },
    argv: ['search_lab/beir_eval.py', '--dataset', 'scifact', '--vectors'],
    performance: {
      queries: 300,
      scoring_s: 55,
      qps: 5.45,
      mean_ms: 183.3,
      max_ms: 1060.8,
      total: { p50: 170.7, p95: 296.9, p99: 377.2 },
      stages: {
        rank_ms: { p50: 118.8, p95: 237.9, p99: 309.4 },
        fts_ms: { p50: 43.9, p95: 73.2, p99: 100 },
      },
      staged: 300,
      cold: 0,
      pool_p50: 1000,
      corpus_docs: 5183,
      arms: ['lexical', 'vectors'],
    },
    has_queries: true,
    on_bench: true,
    current: true,
    code_current: false,
    measure_keys: ['ndcg10', 'mrr10'],
    ...over,
  }
}

/** One run's per-query detail: a query whose gold never came back, one ranked
 *  too deep to count, and one served well — the three states the table has to
 *  tell apart, two of which score identically. */
export function queries(over: Partial<RunQueries> = {}): RunQueries {
  const rows = over.rows ?? [
    {
      qid: 'miss',
      query: 'what did we decide about the retry budget',
      latency_ms: 240.5,
      rank: null,
      n_gold: 2,
      found: 0,
      measures: { ndcg10: 0, mrr10: 0 },
    },
    {
      qid: 'deep',
      query: 'where does the pool cache get invalidated',
      latency_ms: 180.1,
      rank: 40,
      n_gold: 1,
      found: 0,
      measures: { ndcg10: 0, mrr10: 0 },
      group: 'multi-hop',
    },
    {
      qid: 'ok',
      query: 'how is the corpus fingerprinted',
      latency_ms: 120,
      rank: 1,
      n_gold: 1,
      found: 1,
      measures: { ndcg10: 1, mrr10: 1 },
    },
  ]
  return {
    run_id: 'aaaa11112222',
    compared_to: null,
    order: 'worst',
    lead: 'ndcg10',
    total: rows.length,
    misses: rows.filter((r) => r.rank === null).length,
    returned: rows.length,
    rows,
    ...over,
  }
}

export function ledger(over: Partial<BenchRuns> = {}): BenchRuns {
  const runs = over.runs ?? [
    run(),
    run({
      id: 'bbbb33334444',
      at: '2026-07-26T22:10:04+00:00',
      code_id: '11112222aaaabbbb',
      measures: { n: 300, ndcg10: 0.681, mrr10: 0.64, recall100: 0.9 },
      // Slower everywhere at the earlier configuration — so the newer run has a
      // movement in cost to show beside its movement in score.
      performance: {
        queries: 300,
        scoring_s: 70,
        qps: 4.29,
        mean_ms: 230.1,
        max_ms: 1400,
        total: { p50: 210.4, p95: 350.2, p99: 480.6 },
        stages: {
          rank_ms: { p50: 150.2, p95: 280, p99: 400 },
          fts_ms: { p50: 50.1, p95: 80, p99: 110 },
        },
        staged: 300,
        cold: 1,
        pool_p50: 1000,
        corpus_docs: 5183,
        arms: ['lexical', 'vectors'],
      },
      current: false,
    }),
    run({
      id: 'cccc55556666',
      has_queries: false,
      at: '2026-07-26T20:02:11+00:00',
      row: 'gold-gate:swe-chat',
      status: 'failed',
      measures: {},
      measure_keys: [],
      performance: null,
      on_bench: false,
      current: false,
      code_current: null,
    }),
  ]
  return { code_id: 'e60a586e0b84147c', total: runs.length, returned: runs.length,
           runs, ...over }
}

/** The page makes two calls — the inventory and the ledger — so both are stubbed
 *  here; MSW fails a test on any request no handler matches. */
function view(runs: BenchRuns = ledger()) {
  mswJson('/api/search-lab/runs', runs)
  return render(
    <MemoryRouter>
      <SearchLabView />
    </MemoryRouter>,
  )
}

/** The row a benchmark/dataset name sits in, so an assertion about one row cannot
 *  be satisfied by text somewhere else on the page. The run ledger names the same
 *  rows — once per recorded run, and again on its filter chips — so the search
 *  skips that section: these assertions are about the summary tables. */
function rowFor(name: string): HTMLElement {
  const runs = screen.queryByRole('heading', { name: 'Runs' })?.closest('section')
  const cell = screen.getAllByText(name).find((el) => !runs?.contains(el))
  if (!cell) throw new Error(`no summary row for ${name}`)
  return cell.closest('tr') as HTMLElement
}

it('says what a benchmark number here does not claim', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  await screen.findByRole('heading', { name: 'Search lab', level: 1 })
  // Every row is a public benchmark, and the page has to say so where the numbers
  // are: read without the caveat, a table of nDCG figures on an operator's own
  // dashboard reads as "search on this archive scores 0.709".
  const bench = within(screen.getByRole('heading', { name: 'Benchmarks' }).closest('section')!)
  expect(bench.getByText(/never whether search got better/i)).toBeInTheDocument()
  expect(bench.getByText(/would have to be made by searching this archive/i)).toBeInTheDocument()
  expect(within(rowFor('beir:scifact[vectors]')).getByText('0.709')).toBeInTheDocument()
})

it('says a row cannot run when its corpus is absent, and how to build it', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const row = within(await waitFor(() => rowFor('beir:nfcorpus[lexical]')))
  // "no corpus" rather than "stale": the row cannot run at all, and calling it
  // stale would suggest re-running is what it needs.
  expect(row.getByText('no corpus')).toBeInTheDocument()
  expect(row.getByText(/builds it on first run/i)).toBeInTheDocument()
})

it('distinguishes a built corpus from one merely downloaded and one not here', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  await screen.findByRole('heading', { name: 'Datasets' })
  expect(within(rowFor('scifact')).getByText('built')).toBeInTheDocument()
  expect(within(rowFor('arguana')).getByText('available')).toBeInTheDocument()
  // A built corpus reports what it holds, so the page answers "is this worth
  // scoring on?" without opening the home.
  expect(within(rowFor('scifact')).getByText(/5,183 threads/)).toBeInTheDocument()
})

it('marks which miners fixed their labels outside retrieval', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const heading = await screen.findByRole('heading', { name: 'Miners' })
  const miners = within(heading.closest('section') as HTMLElement)
  expect(miners.getByText('retrieval-free labels')).toBeInTheDocument()
  expect(miners.getByText('pooled labels')).toBeInTheDocument()
  // What fixed the labels, not just that they exist: it is the field that decides
  // what a number scored against them may be claimed to mean.
  expect(miners.getByText(/commit provenance/)).toBeInTheDocument()
  expect(miners.getByText(/never run here/)).toBeInTheDocument()
})

it('says nothing gates on what the miners mint', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  // The page lists the miners because they exist and cost tokens, not because
  // their output grades anything. Passing the retrieval-free rung is the ceiling
  // on what a mined case may claim, and the ceiling is well short of a gate.
  const heading = await screen.findByRole('heading', { name: 'Miners' })
  const miners = within(heading.closest('section') as HTMLElement)
  expect(miners.getByText(/Nothing gates on what these produce/i)).toBeInTheDocument()
  expect(miners.getByText(/never a number a change can be credited against/i)).toBeInTheDocument()
})

it('reports a truncated size as a floor, never as the total', async () => {
  mswJson(
    '/api/search-lab',
    inventory({ cache: { bytes: 26_000_000_000, files: 60_000, truncated: true } }),
  )
  view()

  expect(await screen.findByText('≥26 GB')).toBeInTheDocument()
})

it('explains itself when the lab is not installed', async () => {
  mswError('/api/search-lab', 404, 'the bench inventory ships with the search lab')
  view()

  expect(await screen.findByText(/Could not load the bench inventory/)).toBeInTheDocument()
})

// ── the run ledger ──────────────────────────────────────────────────────────

/** The runs section, which is the only place a superseded pass or a failure is
 *  visible — the table above it keeps one run per row by construction. */
function runsSection(): HTMLElement {
  return screen.getByRole('heading', { name: 'Runs' }).closest('section') as HTMLElement
}

/** Just the ledger table. The section also carries a filter chip per row, which
 *  names every row a second time — an assertion about what the ledger *holds*
 *  must not be satisfiable by the control that narrows it. */
function runsTable(): HTMLElement {
  return runsSection().querySelector('table') as HTMLElement
}

it('shows every recorded run, not just the newest of each row', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const runs = within(await waitFor(runsTable))
  // Two passes of one row: the summary above can only ever show the newer, and
  // the older one is the configuration its delta was read against.
  expect(runs.getAllByText('beir:scifact[vectors]')).toHaveLength(2)
  expect(runs.getByText('reported')).toBeInTheDocument()
  expect(runs.getByText('superseded')).toBeInTheDocument()
})

it('keeps a failed run visible', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  // A summary of successes renders a row that stopped being runnable as a
  // silent gap — the ledger is where "this failed at this configuration" lives.
  const runs = within(await waitFor(runsTable))
  expect(runs.getByText('failed')).toBeInTheDocument()
})

it('keeps runs of a row that has left the bench, and says it has', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const runs = within(await waitFor(runsTable))
  // Its numbers were measured on this box and have nowhere else to be read; the
  // manifest no longer names it, so the page cannot let it read as current.
  expect(runs.getByText('gold-gate:swe-chat')).toBeInTheDocument()
  expect(runs.getByText(/no longer a row on the bench/)).toBeInTheDocument()
})

it('marks the run measured under the code in the working tree', async () => {
  mswJson('/api/search-lab', inventory())
  view(ledger({ runs: [run({ code_current: true })] }))

  // The only state under which a recorded number still describes what a run
  // today would produce — and the hashes are too long to compare by eye.
  const runs = within(await waitFor(runsTable))
  expect(runs.getByText('in tree')).toBeInTheDocument()
})

it('links each run to the run itself', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const runs = within(await waitFor(runsTable))
  const links = runs.getAllByRole('link')
  expect(links.map((a) => a.getAttribute('href'))).toContain('/lab/run/aaaa11112222')
  // And the summary row above leads to the same record, so a number a reader is
  // already looking at is a way in rather than something to find again below.
  const summary = within(rowFor('beir:scifact[vectors]'))
  expect(summary.getByRole('link').getAttribute('href')).toBe('/lab/run/aaaa11112222')
})

it('narrows the ledger to one row', async () => {
  const { default: userEvent } = await import('@testing-library/user-event')
  mswJson('/api/search-lab', inventory())
  view()

  const section = within(await waitFor(runsSection))
  await userEvent.click(section.getByRole('button', { name: 'gold-gate:swe-chat' }))
  // Scoped, the table drops the row column entirely — repeating one name down
  // every line of a table already showing that row says nothing.
  const runs = within(runsTable())
  expect(runs.queryByText('beir:scifact[vectors]')).toBeNull()
  expect(runs.getByText('failed')).toBeInTheDocument()
})

it('survives a ledger it cannot read without losing the inventory', async () => {
  mswJson('/api/search-lab', inventory())
  mswError('/api/search-lab/runs', 500, 'nope')
  render(
    <MemoryRouter>
      <SearchLabView />
    </MemoryRouter>,
  )

  // Two routes, two costs, two failures: the history is a file read and the
  // inventory a cached filesystem walk, and neither may take the other down.
  expect(await screen.findByText(/could not be read/i)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Benchmarks' })).toBeInTheDocument()
})

it('draws a mining run as a funnel, with each stage’s losses named', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const heading = await screen.findByRole('heading', { name: 'Miners' })
  const miners = within(heading.closest('section') as HTMLElement)
  const funnel = within(miners.getByRole('list', { name: 'mining funnel' }))

  // Every stage of the recorded run, in order — including the two supply steps,
  // which happen before any agent runs and are most of the narrowing.
  const stages = funnel.getAllByRole('listitem').map((li) => li.querySelector('code')?.textContent)
  expect(stages).toEqual(['linkage', 'sample', 'provenance', 'alignment', 'author'])
  expect(funnel.getByText(/1,284 → 1,149/)).toBeInTheDocument()
  expect(funnel.getByText(/absent-from-snapshot 135/)).toBeInTheDocument()

  // The two paid drops stay apart: one is a wrong label leaving the benchmark,
  // the other a hard case leaving it, and they move a number opposite ways.
  expect(funnel.getByText(/misattributed 1 · untargetable-commit 2/)).toBeInTheDocument()
  // Spend is attributed to the stage that incurred it, not just totalled.
  expect(funnel.getByText(/\$7\.10/)).toBeInTheDocument()
})

it('says a miner without stages records where units ended, not where they were lost', async () => {
  mswJson('/api/search-lab', inventory())
  view()

  const heading = await screen.findByRole('heading', { name: 'Miners' })
  const miners = within(heading.closest('section') as HTMLElement)
  // `pooled` declares no stages. That must read as "not recorded" rather than as
  // a clean run in which nothing was dropped.
  expect(miners.getByText(/one opaque step/)).toBeInTheDocument()
  expect(miners.getByText(/never where they were lost/)).toBeInTheDocument()
})
