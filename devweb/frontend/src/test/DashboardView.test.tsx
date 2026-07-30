import { expect, it } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import type {
  BenchRunRecord,
  BenchRuns,
  Benchmark,
  LabInventory,
  RetrievalReport,
  TelemetryReport,
} from '../api'
import { DashboardView } from '../components/DashboardView'
import { mswError, mswJson, recordRequests } from './msw'

// The overview reads all four endpoints the other three views read between them,
// so every test stubs all four and overrides only the payload it is about — MSW
// fails a test on any request no handler matches.
//
// Its own fixtures rather than the other views': importing one of those files
// registers that file's tests into this one, and a suite that runs three other
// suites again reports every failure in them three times.

function retrieval(over: Partial<RetrievalReport> = {}): RetrievalReport {
  return {
    home: '/Users/test/.thread/archive',
    hours: 24,
    bucket: 'hour',
    at: '2026-07-29T20:00:00Z',
    served: {
      hours: 24,
      bucket: 'hour',
      n: 60,
      n_unknown_regime: 0,
      buckets: [],
      warm: { n: 54, p50: 228, p90: 860, p99: 2415 },
      warm_interactive: { n: 48, p50: 205, p90: 640, p99: 1900 },
      warm_bulk: { n: 6, p50: 4100, p90: 9800, p99: 11200 },
      cold: { n: 6, p50: 8600, p90: 12500, p99: 13100 },
      // A warmed server, and a door that is cold by construction: one process
      // per search, so it has no typical warm search at all.
      by_surface: [
        {
          surface: 'mcp-http', n: 56, n_cold: 2, p50: 240, p90: 900,
          warm_interactive: { n: 48, p50: 190, p90: 600, p99: 1500 },
          warm_bulk: { n: 6, p50: 3900, p90: 9000 },
          cold: { n: 2, p50: 8200, p90: 9000 },
        },
        {
          surface: 'cli', n: 4, n_cold: 4, p50: 8600, p90: 12500,
          warm_interactive: { n: 0, p50: 0, p90: 0, p99: 0 },
          warm_bulk: { n: 0, p50: 0, p90: 0 },
          cold: { n: 4, p50: 8600, p90: 12500 },
        },
      ],
    },
    stages: {
      n: 54,
      n_unproven: 0,
      stages: [
        { stage: 'semantic_ms', n: 54, p50: 90, p90: 293 },
        { stage: 'fts_ms', n: 54, p50: 178, p90: 1344 },
      ],
    },
    restarts: {
      n: 3, bucket: 'hour', buckets: [{ at: '2026-07-29T18', n: 3 }],
      by_surface: [{ surface: 'mcp-http', n: 3, p50_ms: 22300 }],
      p50_ms: 22300, total_s: 67,
    },
    bench: null,
    recording: true,
    ...over,
  }
}

function telemetry(over: Partial<TelemetryReport> = {}): TelemetryReport {
  return {
    home: '/Users/test/.thread/archive',
    hours: 24,
    at: '2026-07-29T20:00:00Z',
    web: {
      requests: 42,
      errors: 2,
      bytes: 100_000,
      concurrent: 7,
      p50: 12,
      p95: 420,
      p99: 930,
      max: 1200,
      retained_bytes: 4_000_000,
      endpoints: [
        {
          method: 'GET', path: '/api/search', n: 3, errors: 0, bytes: 20_000,
          concurrent: 2, p50: 320, p95: 930, p99: 930, max: 930,
        },
        {
          method: 'GET', path: '/api/status', n: 39, errors: 2, bytes: 80_000,
          concurrent: 5, p50: 10, p95: 30, p99: 40, max: 40,
        },
      ],
    },
    ingest: {
      hours: 24,
      sources: {
        codex: {
          passes: 12, items: 12, events: 88, lines: 400, bytes: 250_000,
          errors: 1, pass_p50_ms: 14, pass_p95_ms: 35, total_s: 0.2,
        },
      },
      stages: { write_ms: 80, parse_ms: 40 },
      maintenance: { passes: 2, total_s: 0.5, p95_ms: 300 },
      embed: { passes: 1, embedded: 20, total_s: 2.5, p95_ms: 2500 },
      retained_bytes: 900_000,
    },
    faults: [
      {
        signature: 'codex: could not parse <path>',
        source: 'codex',
        count: 100,
        first: '2026-07-20T10:00:00Z',
        last: '2026-07-29T10:00:00Z',
        sample: 'codex: could not parse /tmp/example',
      },
    ],
    ledgers: [
      { file: 'web-requests.jsonl', label: 'web requests', view: 'telemetry', bytes: 4_000_000, segments: 1, recording: true },
      { file: 'retrieval-usage.jsonl', label: 'retrieval calls', view: 'retrieval', bytes: 450_000, segments: 2, recording: true },
    ],
    recording: true,
    ...over,
  }
}

function benchmark(over: Partial<Benchmark> = {}): Benchmark {
  return {
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
    ...over,
  }
}

/** One row that can run and one whose corpus is absent, over one built corpus
 *  and two that are not — the states the overview's pills and its facts line
 *  have to tell apart. */
function inventory(over: Partial<LabInventory> = {}): LabInventory {
  return {
    cache_root: '/Users/test/.cache/thread-evals',
    cache: { bytes: 26_000_000_000, files: 84_000, truncated: false },
    code_id: 'e60a586e0b84147c',
    families: { beir: 'public IR benchmarks' },
    benchmarks: [
      benchmark(),
      benchmark({
        name: 'beir:nfcorpus[lexical]',
        corpus_built: false,
        state: 'missing',
        measure_keys: ['ndcg10'],
        last: null,
      }),
    ],
    datasets: [
      {
        name: 'scifact',
        family: 'beir',
        harness: 'search_lab/beir_eval.py --dataset scifact',
        download: { path: '/scifact', present: true, bytes: 8_000_000 },
        homes: [
          {
            label: 'corpus', path: '/homes/scifact', built: true,
            snapshot_id: '9b7bce63', counts: { threads: 5183, vectors: 5848 },
            embedding_space: 'local:nomic', created_at: '2026-07-27T03:42:18Z',
            bytes: 92_300_000,
          },
        ],
        reference: { metric: 'nDCG@10', bm25: 0.665 },
        on_bench: ['beir:scifact[vectors]'],
      },
      {
        name: 'nfcorpus',
        family: 'beir',
        harness: 'search_lab/beir_eval.py --dataset nfcorpus',
        download: { path: '/nfcorpus', present: true, bytes: 6_000_000 },
        homes: [
          {
            label: 'corpus', path: '/homes/nfcorpus', built: false,
            snapshot_id: null, counts: {}, embedding_space: null, created_at: null,
          },
        ],
        reference: { metric: 'nDCG@10', bm25: 0.325 },
        on_bench: ['beir:nfcorpus[lexical]'],
      },
      {
        name: 'scidocs',
        family: 'beir',
        harness: 'search_lab/beir_eval.py --dataset scidocs',
        download: { path: '/scidocs', present: false },
        homes: [
          {
            label: 'corpus', path: '/homes/scidocs', built: false,
            snapshot_id: null, counts: {}, embedding_space: null, created_at: null,
          },
        ],
        reference: { metric: 'nDCG@10', bm25: 0.158 },
        on_bench: [],
      },
    ],
    ...over,
  }
}

function run(over: Partial<BenchRunRecord> = {}): BenchRunRecord {
  return {
    id: 'aaaa11112222',
    at: '2026-07-27T06:45:22+00:00',
    row: 'beir:scifact[vectors]',
    status: 'ok',
    code_id: 'd46b8e7f00000000',
    corpus_id: '9b7bce63',
    commit: 'abc1234',
    elapsed_s: 59.3,
    measures: { n: 300, ndcg10: 0.709, mrr10: 0.667 },
    argv: ['search_lab/beir_eval.py', '--dataset', 'scifact', '--vectors'],
    has_queries: true,
    on_bench: true,
    current: true,
    code_current: false,
    measure_keys: ['ndcg10', 'mrr10'],
    ...over,
  }
}

/** A pass that is reported, and a failure of a row the manifest no longer
 *  names — the ledger's own two states, which the summary tables cannot show. */
function ledger(over: Partial<BenchRuns> = {}): BenchRuns {
  const runs = over.runs ?? [
    run(),
    run({
      id: 'cccc55556666',
      at: '2026-07-26T20:02:11+00:00',
      row: 'beir:scifact[lexical+rerank]',
      status: 'failed',
      measures: {},
      measure_keys: [],
      on_bench: false,
      current: false,
      code_current: null,
    }),
  ]
  return {
    code_id: 'e60a586e0b84147c',
    total: runs.length,
    returned: runs.length,
    runs,
    ...over,
  }
}

function view(over: {
  retrieval?: RetrievalReport
  telemetry?: TelemetryReport
  lab?: LabInventory
  runs?: BenchRuns
} = {}) {
  mswJson('/api/retrieval', over.retrieval ?? retrieval())
  mswJson('/api/telemetry', over.telemetry ?? telemetry())
  mswJson('/api/search-lab', over.lab ?? inventory())
  mswJson('/api/search-lab/runs', over.runs ?? ledger())
  return render(
    <MemoryRouter>
      <DashboardView />
    </MemoryRouter>,
  )
}

/** The panel a heading names, so an assertion about one panel cannot be
 *  satisfied by a number in the panel beside it. */
function panel(title: string): HTMLElement {
  return screen.getByRole('heading', { name: title }).closest('section') as HTMLElement
}

it('carries the door table across intact, and quotes no median pooled over doors', async () => {
  view()

  await screen.findByRole('heading', { name: 'Front doors' })
  const doors = within(panel('Front doors'))
  // Each door's own typical search: the shared server's, and the terminal's —
  // which has none, because every search it serves is that process's first.
  expect(doors.getByText('mcp-http')).toBeInTheDocument()
  expect(doors.getByText('190ms')).toBeInTheDocument()
  const cli = doors.getByText('cli').closest('tr') as HTMLElement
  expect(within(cli).getByText('—')).toBeInTheDocument()
  expect(within(cli).getByText('8.60s')).toBeInTheDocument()
  // The pooled row the retrieval page keeps last and names as a mixture is the
  // one number a dashboard would be tempted to lead with. It is not here at all.
  expect(screen.queryByText(/all doors/)).not.toBeInTheDocument()
})

it('prices the window’s restarts in the warm-up they cost', async () => {
  view()

  // The largest single influence on what a caller felt, and invisible in every
  // latency number on the page.
  expect(await screen.findByText(/3 process starts cost 67s of warm-up/)).toBeInTheDocument()
})

it('orders the stages slowest first and says they do not sum to a search', async () => {
  view()

  await screen.findByRole('heading', { name: 'Where the time goes' })
  const stages = within(panel('Where the time goes'))
  // The server's order is not load-bearing here: the panel shows the slowest
  // six, so it sorts them itself.
  expect(stages.getAllByText(/_ms$/).map((el) => el.textContent)).toEqual([
    'fts_ms',
    'semantic_ms',
  ])
  expect(stages.getByText(/do not add up to a search/)).toBeInTheDocument()
})

it('shows the slowest endpoints beside the count of requests that failed', async () => {
  view()

  await screen.findByRole('heading', { name: 'Web requests' })
  const web = within(panel('Web requests'))
  expect(web.getByText('42')).toBeInTheDocument()
  expect(web.getByText('errors')).toBeInTheDocument()
  expect(web.getByText('/api/search')).toBeInTheDocument()
})

it('names the retained fault rather than only counting it', async () => {
  view()

  await screen.findByRole('heading', { name: 'Ingest' })
  const ingest = within(panel('Ingest'))
  expect(ingest.getByText('codex: could not parse <path>')).toBeInTheDocument()
  // And says that count is not a count of this window, which is what every other
  // number in the panel is.
  expect(ingest.getByText(/not bounded by this window/)).toBeInTheDocument()
})

it('keeps a source’s errors visible beside the work it did', async () => {
  view()

  await screen.findByRole('heading', { name: 'Ingest' })
  const row = within(panel('Ingest')).getByText('codex').closest('tr') as HTMLElement
  expect(within(row).getByText('88')).toBeInTheDocument()
  expect(row.querySelector('.telemetry-bad')?.textContent).toBe('1')
})

it('keeps the panels whose ledgers still read when one cannot be read', async () => {
  mswError('/api/retrieval', 500, 'the retrieval ledger is mid-rebuild')
  mswJson('/api/telemetry', telemetry())
  mswJson('/api/search-lab', inventory())
  mswJson('/api/search-lab/runs', ledger())
  render(
    <MemoryRouter>
      <DashboardView />
    </MemoryRouter>,
  )

  // Named, because four panels load separately and "could not load" alone leaves
  // the reader to work out which ledger is unreadable.
  expect(await screen.findAllByText(/Could not load the retrieval report/)).toHaveLength(2)
  expect(screen.getByText('/api/search')).toBeInTheDocument()
  expect(screen.getAllByText('beir:scifact[vectors]').length).toBeGreaterThan(0)
})

it('moves both windowed panels to the same window, and re-walks nothing else', async () => {
  const seen = recordRequests()
  view()
  await screen.findByRole('heading', { name: 'Front doors' })

  await userEvent.selectOptions(screen.getByRole('combobox'), '168')

  // Read side by side, retrieval and telemetry have to cover the same stretch of
  // time or the two halves of the screen answer about different afternoons.
  await waitFor(() => {
    expect(seen).toContain('/api/retrieval?hours=168')
    expect(seen).toContain('/api/telemetry?hours=168')
  })
  // The inventory is a walk over tens of GB and describes the box rather than a
  // window; the ledger is the box's whole history. Neither moves with the range.
  expect(seen.filter((path) => path === '/api/search-lab')).toHaveLength(1)
  expect(seen.filter((path) => path === '/api/search-lab/runs')).toHaveLength(1)
})

it('says how many bench rows and runs it is not showing', async () => {
  view({
    lab: inventory({
      benchmarks: Array.from({ length: 12 }, (_, i) => benchmark({ name: `row-${i}` })),
    }),
    runs: ledger({ total: 85 }),
  })

  // A capped table that does not say it is capped reads as the whole ledger.
  expect(await screen.findByText('+4 more rows')).toBeInTheDocument()
  expect(screen.getByText('+77 more runs')).toBeInTheDocument()
})

it('names the state each bench row is in, and links a run to the run itself', async () => {
  view()

  await screen.findByRole('heading', { name: 'The bench' })
  const bench = within(panel('The bench'))
  // The tinted chip, not the word in the gloss under it.
  expect(bench.getAllByText('stale').map((el) => el.className)).toContain('lab-pill warn')
  expect(bench.getByText('no corpus')).toBeInTheDocument()
  expect(bench.getByText('failed')).toBeInTheDocument()
  const runs = bench
    .getAllByRole('link')
    .map((link) => link.getAttribute('href'))
    .filter((href) => href?.startsWith('/lab/run/'))
  expect(runs).toContain('/lab/run/aaaa11112222')
})

it('opens onto the page behind every panel', async () => {
  view()

  await screen.findByRole('heading', { name: 'Front doors' })
  // A number with nowhere to go is where a dashboard starts being read instead
  // of the instrument it summarises.
  for (const [label, href] of [
    ['retrieval →', '/retrieval'],
    ['telemetry →', '/telemetry'],
    ['lab →', '/lab'],
  ] as const)
    for (const link of screen.getAllByRole('link', { name: label }))
      expect(link).toHaveAttribute('href', href)
})

it('opens on the window the instruments themselves open on', async () => {
  const seen = recordRequests()
  view()

  // Both pages default to this too. A panel quoting one stretch of time beside a
  // page reporting another shows the same table with different numbers in it,
  // which reads as the two disagreeing.
  await screen.findByRole('heading', { name: 'Front doors' })
  expect(seen).toContain('/api/retrieval?hours=336')
  expect(seen).toContain('/api/telemetry?hours=336')
  expect(screen.getByRole('combobox')).toHaveValue('336')
})

it('says where this box keeps what the panels measured', async () => {
  view()

  expect(await screen.findByText(/corpora 1\/3 built/)).toBeInTheDocument()
  expect(screen.getByText('/Users/test/.thread/archive')).toBeInTheDocument()
})
