import type { Page, Route } from '@playwright/test'

/**
 * Browser-test fixtures for the dev panels.
 *
 * Every API call is answered here, at Chromium's network boundary, so the lane
 * is deterministic and never reads the operator's live archive or the bench's
 * real run ledger. An unmocked request lands in the returned `unhandled` list
 * and fails the spec, so a view that grows a new call cannot pass quietly.
 */

export const RUN_ID = 'aa11bb22cc33'

/** A fixed instant, so a fixture's numbers never depend on when the suite ran. */
const now = '2026-07-20T12:00:00Z'

// The retrieval page reads three ledgers; the fixture carries one warm day and one
// cold search so both regimes render — a single-regime fixture would let the page
// ship with the blend it exists to avoid. One empty bucket too: the span is dense,
// and a page that cannot draw a gap would pass a fixture that has none.
const retrieval = {
  home: '/Users/test/.thread/archive',
  hours: 14 * 24,
  bucket: 'day',
  at: now,
  served: {
    hours: 14 * 24,
    bucket: 'day',
    n: 24,
    n_unknown_regime: 4,
    buckets: [
      { at: '2026-07-19', n: 12, warm: { n: 11, p50: 240, p90: 900 }, cold: { n: 1, p50: 8200, p90: 8200 } },
      { at: '2026-07-20', n: 0 },
      { at: '2026-07-21', n: 12, warm: { n: 11, p50: 210, p90: 800 }, cold: { n: 1, p50: 9100, p90: 9100 } },
    ],
    warm: { n: 22, p50: 228, p90: 860, p99: 2415 },
    warm_interactive: { n: 18, p50: 205, p90: 640, p99: 1900 },
    warm_bulk: { n: 4, p50: 4100, p90: 9800, p99: 11200 },
    cold: { n: 2, p50: 8600, p90: 9100, p99: 9100 },
    by_surface: [
      {
        surface: 'mcp-http', n: 22, n_cold: 0, p50: 228, p90: 860,
        warm_interactive: { n: 18, p50: 205, p90: 640, p99: 1900 },
        warm_bulk: { n: 4, p50: 4100, p90: 9800 },
        cold: { n: 0, p50: 0, p90: 0 },
      },
      {
        surface: 'cli', n: 2, n_cold: 2, p50: 8600, p90: 9100,
        warm_interactive: { n: 0, p50: 0, p90: 0, p99: 0 },
        warm_bulk: { n: 0, p50: 0, p90: 0 },
        cold: { n: 2, p50: 8600, p90: 9100 },
      },
    ],
  },
  stages: {
    n: 22,
    n_unproven: 4,
    stages: [
      { stage: 'fts_ms', n: 22, p50: 178, p90: 1344 },
      { stage: 'semantic_ms', n: 22, p50: 90, p90: 293 },
    ],
  },
  restarts: {
    n: 3, bucket: 'day', buckets: [{ at: '2026-07-20', n: 3 }],
    by_surface: [{ surface: 'mcp-http', n: 2, p50_ms: 22300 },
                 { surface: 'web', n: 1, p50_ms: 15300 }],
    p50_ms: 22300, total_s: 67,
  },
  bench: {
    observed: [{ at: now, commit: 'abc1234', p50: 228, p95: 1412, p99: 2415, n_queries: 40, tuning: false }],
  },
}

const telemetry = {
  home: '/tmp/browser-archive',
  hours: 24,
  at: now,
  web: {
    requests: 84,
    errors: 1,
    bytes: 260_000,
    concurrent: 12,
    p50: 14,
    p95: 410,
    p99: 960,
    max: 1250,
    retained_bytes: 4_500_000,
    endpoints: [
      {
        method: 'GET',
        path: '/api/search-lab',
        n: 4,
        errors: 0,
        bytes: 90_000,
        concurrent: 2,
        p50: 340,
        p95: 960,
        p99: 960,
        max: 960,
      },
      {
        method: 'GET',
        path: '/api/status',
        n: 80,
        errors: 1,
        bytes: 170_000,
        concurrent: 10,
        p50: 12,
        p95: 35,
        p99: 80,
        max: 120,
      },
    ],
  },
  ingest: {
    hours: 24,
    sources: {
      codex: {
        passes: 18,
        items: 18,
        events: 142,
        lines: 790,
        bytes: 520_000,
        errors: 0,
        pass_p50_ms: 14,
        pass_p95_ms: 38,
        total_s: 0.4,
      },
    },
    stages: { write_ms: 120, parse_ms: 72, fts_ms: 40 },
    maintenance: { passes: 3, total_s: 1.2, p95_ms: 580 },
    embed: { passes: 2, embedded: 42, total_s: 4.8, p95_ms: 2700 },
    retained_bytes: 930_000,
  },
  faults: [
    {
      signature: 'codex: failed to read <path>',
      source: 'codex',
      count: 10,
      first: '2026-07-18T10:00:00Z',
      last: now,
      sample: 'codex: failed to read /tmp/session.jsonl',
    },
  ],
  ledgers: [
    { file: 'web-requests.jsonl', label: 'web requests', view: 'telemetry', bytes: 4_500_000, segments: 1 },
    { file: 'ingest-runs.jsonl', label: 'ingest work', view: 'telemetry', bytes: 930_000, segments: 1 },
    { file: 'retrieval-usage.jsonl', label: 'retrieval calls', view: 'retrieval', bytes: 460_000, segments: 1 },
    { file: 'load-runs.jsonl', label: 'load runs', view: 'health', bytes: 12_000, segments: 1 },
    { file: 'ingest-errors.jsonl', label: 'ingest faults', view: 'telemetry', bytes: 2_000, segments: 1 },
  ],
}

// The lab page is an inventory, so the fixture's job is to carry one of every
// *state* rather than a plausible bench: a benchmark row that can run and one
// whose corpus is absent, and a corpus built / merely downloaded / not here at
// all. Every branch the page renders is a branch a fixture with one uniform row
// would let ship broken.
const searchLab = {
  cache_root: '/Users/test/.cache/thread-evals',
  cache: { bytes: 26_000_000_000, files: 84_000, truncated: false },
  code_id: 'e60a586e0b84147c',
  families: {
    beir: 'public IR benchmarks — nDCG@10 beside published references',
  },
  benchmarks: [
    {
      name: 'beir:scifact[vectors]',
      argv: ['search_lab/beir_eval.py'],
      corpus_home: '/Users/test/.cache/thread-evals/homes/scifact',
      corpus_id: '9b7bce6344de1ad8',
      corpus_built: true,
      build_hint: 'the harness builds it on first run (ingest + embed)',
      cost_min: 5,
      est_min: 1,
      fresh: false,
      state: 'stale',
      measure_keys: ['ndcg10', 'mrr10'],
      code_id: 'f13d940f36263c9d',
      last: {
        at: now,
        elapsed_s: 59.3,
        commit: 'abc1234',
        code_id: 'd46b8e7f11223344',
        measures: { ndcg10: 0.709, mrr10: 0.667 },
      },
    },
    {
      name: 'beir:nfcorpus[lexical]',
      argv: ['search_lab/beir_eval.py'],
      corpus_home: '/Users/test/.cache/thread-evals/homes/nfcorpus',
      corpus_id: null,
      corpus_built: false,
      build_hint: 'the harness builds it on first run (ingest + embed)',
      cost_min: 2,
      est_min: 2,
      fresh: false,
      state: 'missing',
      measure_keys: ['ndcg10'],
      code_id: 'f13d940f36263c9d',
      last: null,
    },
  ],
  datasets: [
    {
      name: 'scifact',
      family: 'beir',
      harness: 'search_lab/beir_eval.py --dataset scifact',
      download: { path: '/Users/test/.cache/thread-evals/scifact', present: true, bytes: 8_000_000 },
      homes: [
        {
          label: 'corpus',
          path: '/Users/test/.cache/thread-evals/homes/scifact',
          built: true,
          snapshot_id: '9b7bce6344de1ad8',
          counts: { threads: 5183, vectors: 5848 },
          embedding_space: 'local:nomic-ai/nomic-embed-text-v1.5',
          created_at: now,
          bytes: 92_300_000,
          files: 5200,
          truncated: false,
          build: { docs: 5183, embedded: true },
        },
      ],
      reference: { metric: 'nDCG@10', bm25: 0.665, dense: 0.68 },
      on_bench: ['beir:scifact[lexical]'],
    },
    {
      name: 'nfcorpus',
      family: 'beir',
      harness: 'search_lab/beir_eval.py --dataset nfcorpus',
      download: { path: '/Users/test/.cache/thread-evals/nfcorpus', present: true, bytes: 6_000_000 },
      homes: [
        {
          label: 'corpus',
          path: '/Users/test/.cache/thread-evals/homes/nfcorpus',
          built: false,
          snapshot_id: null,
          counts: {},
          embedding_space: null,
          created_at: null,
        },
      ],
      reference: { metric: 'nDCG@10', bm25: 0.325, dense: 0.33 },
      on_bench: ['beir:nfcorpus[lexical]'],
    },
    {
      name: 'scidocs',
      family: 'beir',
      harness: 'search_lab/beir_eval.py --dataset scidocs',
      download: { path: '/Users/test/.cache/thread-evals/scidocs', present: false },
      homes: [
        {
          label: 'corpus',
          path: '/Users/test/.cache/thread-evals/homes/scidocs',
          built: false,
          snapshot_id: null,
          counts: {},
          embedding_space: null,
          created_at: null,
        },
      ],
      reference: { metric: 'nDCG@10', bm25: 0.158, dense: 0.2 },
      on_bench: [],
    },
  ],
}

// The run ledger. Same rule as the inventory above: one of every *state*, since
// this is the surface whose whole reason to exist is the runs a newest-per-row
// summary drops. A reported pass, the superseded pass before it under different
// code (so the delta column renders at all), and a failure of a row the manifest
// no longer names — which is also the only case exercising a null `code_current`.
const searchLabRuns = {
  code_id: 'e60a586e0b84147c',
  total: 3,
  returned: 3,
  runs: [
    {
      id: RUN_ID,
      at: now,
      row: 'beir:scifact[vectors]',
      status: 'ok',
      code_id: 'd46b8e7f11223344',
      corpus_id: '9b7bce6344de1ad8',
      commit: 'abc1234',
      elapsed_s: 59.3,
      measures: { n: 300, ndcg10: 0.709, mrr10: 0.667, recall100: 0.915, query_p50_ms: 172.9 },
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
          semantic_ms: { p50: 0.5, p95: 0.8, p99: 1.2 },
        },
        staged: 300,
        cold: 0,
        pool_p50: 1000,
        corpus_docs: 5183,
        arms: ['lexical', 'vectors'],
      },
      on_bench: true,
      current: true,
      code_current: true,
      has_queries: true,
      measure_keys: ['ndcg10', 'mrr10'],
    },
    {
      id: 'dd44ee55ff66',
      at: '2026-07-19T09:14:02Z',
      row: 'beir:scifact[vectors]',
      status: 'ok',
      code_id: '11112222aaaabbbb',
      corpus_id: '9b7bce6344de1ad8',
      commit: 'def5678',
      elapsed_s: 62.1,
      measures: { n: 300, ndcg10: 0.681, mrr10: 0.64, recall100: 0.9, query_p50_ms: 180.4 },
      argv: ['search_lab/beir_eval.py', '--dataset', 'scifact', '--vectors'],
      // Slower everywhere at the earlier configuration, so the newer run has a
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
          semantic_ms: { p50: 0.6, p95: 0.9, p99: 1.4 },
        },
        staged: 300,
        cold: 1,
        pool_p50: 1000,
        corpus_docs: 5183,
        arms: ['lexical', 'vectors'],
      },
      on_bench: true,
      current: false,
      code_current: false,
      has_queries: true,
      measure_keys: ['ndcg10', 'mrr10'],
    },
    {
      id: '778899aabbcc',
      at: '2026-07-18T22:02:11Z',
      row: 'beir:scifact[lexical+rerank]',
      status: 'failed',
      code_id: '99998888ccccdddd',
      corpus_id: null,
      commit: 'def5678',
      elapsed_s: 4.2,
      measures: {},
      argv: ['search_lab/beir_eval.py', '--dataset', 'scifact', '--rerank'],
      on_bench: false,
      current: false,
      code_current: null,
      // The capped store's other state: a run that keeps its numbers and has
      // lost its detail, which is what the drill-in has to be able to say.
      has_queries: false,
      measure_keys: [],
    },
  ],
}

// One run's per-query detail. Three states the table has to tell apart, two of
// which score identically: a gold document that never came back, one ranked too
// deep to count, and one served well.
const searchLabQueries = {
  run_id: RUN_ID,
  compared_to: null,
  order: 'worst',
  lead: 'ndcg10',
  total: 3,
  misses: 1,
  returned: 3,
  rows: [
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
  ],
}


function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

export async function mockApi(page: Page): Promise<string[]> {
  const unhandled: string[] = []
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/retrieval') return json(route, retrieval)
    if (path === '/api/telemetry') return json(route, telemetry)
    if (path === '/api/search-lab') return json(route, searchLab)
    if (path === '/api/search-lab/runs') return json(route, searchLabRuns)
    if (/^\/api\/search-lab\/runs\/[^/]+\/queries$/.test(path))
      return json(route, searchLabQueries)

    unhandled.push(`${route.request().method()} ${path}`)
    return json(route, { error: 'unhandled browser-test API request' }, 501)
  })
  return unhandled
}

/** Console errors and page exceptions, collected for a spec to assert empty. */
export function monitorPage(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(String(err)))
  return errors
}
