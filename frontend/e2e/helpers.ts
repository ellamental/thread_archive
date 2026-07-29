import type { Page, Route } from '@playwright/test'

export const THREAD_ID = '01ARZ3NDEKTSV4RRFFQ69G5FAV'
export const MODEL = 'claude-opus-4-8'
/** The recorded benchmark run the browser suite opens — the reported pass of a
 *  row still on the bench, which is the case carrying every section of the page. */
export const RUN_ID = 'aa11bb22cc33'

const now = '2026-07-20T12:00:00Z'
const healthNow = new Date().toISOString()

const threadListItem = {
  id: THREAD_ID,
  title: 'Browser Test Thread',
  source: 'claude-code',
  first_user_message: 'Open the browser test thread and verify its recent-card preview.',
  thread_type: 'conversation',
  updated_at: now,
}

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
      { surface: 'mcp-http', n: 22, n_cold: 0, p50: 228, p90: 860 },
      { surface: 'cli', n: 2, n_cold: 2, p50: 8600, p90: 9100 },
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
    by_surface: [{ surface: 'mcp-http', n: 2 }, { surface: 'web', n: 1 }],
    p50_ms: 22300, total_s: 67,
  },
  bench: {
    observed: [{ at: now, commit: 'abc1234', p50: 228, p95: 1412, p99: 2415, n_queries: 40, tuning: false }],
  },
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
      name: 'arguana',
      family: 'beir',
      harness: 'search_lab/beir_eval.py --dataset arguana',
      download: { path: '/Users/test/.cache/thread-evals/arguana', present: false },
      homes: [
        {
          label: 'corpus',
          path: '/Users/test/.cache/thread-evals/homes/arguana',
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

const stats = {
  overview: {
    conversations: 1,
    sources: 1,
    models: 1,
    input_tokens: 1200,
    cache_read_tokens: 4800,
    output_tokens: 300,
    tokens: 1500,
    cost: 0,
    cost_conversations: 0,
    first_at: now,
    last_at: now,
  },
  by_source: [
    {
      source: 'claude-code',
      conversations: 1,
      with_tokens: 1,
      input_tokens: 1200,
      cache_read_tokens: 4800,
      output_tokens: 300,
      tokens: 1500,
      avg_tokens: 1500,
      with_cost: 0,
      cost: null,
      avg_cost: null,
    },
  ],
  by_model: [
    {
      model: MODEL,
      requests: 2,
      input_tokens: 1200,
      cache_read_tokens: 4800,
      output_tokens: 300,
      tokens: 1500,
      cost: null,
      conversations: 1,
    },
  ],
  timeline: {
    months: ['2026-05', '2026-06', '2026-07'],
    undated: 0,
    conversations: [0, 1, 0],
    tokens: [0, 1500, 0],
    cost: [null, null, null],
    conversations_by_source: [{ key: 'claude-code', values: [0, 1, 0] }],
    tokens_by_model: [{ key: MODEL, values: [0, 1500, 0] }],
  },
  session_sizes: {
    buckets: [
      { lo: 0, hi: 1000, count: 0 },
      { lo: 1000, hi: 5000, count: 1 },
      { lo: 5000, hi: null, count: 0 },
    ],
    sessions: 1,
    without_tokens: 0,
    median: 1500,
    p90: 1500,
  },
  rhythm: {
    grid: Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (_, h) => (d === 0 && h === 12 ? 1 : 0))),
    max: 1,
    total: 1,
  },
}

const modelStats = {
  model: MODEL,
  overview: {
    conversations: 1,
    requests: 2,
    input_tokens: 1200,
    cache_read_tokens: 4800,
    output_tokens: 300,
    thinking_tokens: 100,
    tokens: 1500,
    cost: null,
    cost_conversations: 0,
    compactions: 0,
    first_at: now,
    last_at: now,
  },
  per_session: {
    min_tokens: 1500,
    max_tokens: 1500,
    avg_tokens: 1500,
    median_tokens: 1500,
    avg_requests: 2,
  },
  by_month: [
    {
      month: '2026-07',
      sessions: 1,
      requests: 2,
      input_tokens: 1200,
      cache_read_tokens: 4800,
      output_tokens: 300,
      tokens: 1500,
      avg_tokens: 1500,
      cost: null,
      compactions: 0,
    },
  ],
  top_sessions: [
    {
      thread_id: THREAD_ID,
      title: threadListItem.title,
      source: 'claude-code',
      at: now,
      tokens: 1500,
      cache_read_tokens: 4800,
      requests: 2,
      compactions: 0,
    },
  ],
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

/** Install a complete, deterministic API surface for the browser suite. */
export async function mockApi(page: Page): Promise<string[]> {
  const unhandled: string[] = []

  await page.route(/^https?:\/\/[^/]+\/api\//, async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname

    if (path === '/api/status') {
      return json(route, {
        threads: 1,
        events: 3,
        topics: 0,
        links: 0,
        fts_indexed: 3,
        vectors_indexed: 3,
        home: '/tmp/browser-archive',
        truth_dir: '/tmp/browser-archive/truth',
        index_path: '/tmp/browser-archive/index.db',
        last_checkpoint_at: healthNow,
        last_verify: { at: healthNow, ok: true, deep: true, hashes: true, parse_errors: 0 },
        last_backup: {
          at: healthNow,
          ok: true,
          dest: '/Volumes/browser-backup',
          files_copied: 2,
          mirror_complete: true,
        },
        last_restore_drill: { at: healthNow, ok: true, events: 3, seconds: 1 },
        last_nightly: { at: healthNow, ok: true, dest: '/Volumes/browser-backup', failed_stages: [] },
        last_watch_errors: null,
        last_watch_pass: {
          at: healthNow,
          pid: 123,
          passes: 8,
          sources: {
            'claude-code': {
              checked: 2,
              items: 1,
              events: 3,
              lines: 4,
              parse_errors: 0,
              errors: 0,
            },
          },
        },
        last_coverage: {
          at: healthNow,
          ok: true,
          sources_checked: 1,
          failed: [],
          warnings: [],
          skips_recent: 0,
          drift_recent: 0,
        },
        last_source_mirror: { at: healthNow, ok: true, copied: 1, files: 2, bytes_out: 1024, errors: 0 },
        last_self_update: { at: healthNow, ok: true, action: 'up-to-date', current: '0.9.1' },
        source_last_import: { 'claude-code': healthNow },
        pipeline: {
          ran: true,
          ok: true,
          failed_stages: [],
          recovered_stages: [],
          tolerated_stages: [],
          nightly_at: healthNow,
          dest: '/Volumes/browser-backup',
        },
        watch_process_alive: true,
        backup_same_device: false,
      })
    }
    if (path === '/api/notices') {
      // The health page's action queue. One silenced warning: the browser suite
      // is where the indicator and its panel are exercised as a real widget.
      return json(route, {
        active: [],
        silenced: [
          {
            key: 'same-disk',
            tone: 'warn',
            title: 'Backup is on the same filesystem as the archive',
            detail: 'Move the scheduled destination to another disk.',
            command: 'thread-archive daemon install --backup --dest /Volumes/disk',
            fingerprint: 'e2e',
            silenced_at: healthNow,
          },
        ],
      })
    }
    if (path === '/api/loads') {
      // The load ledger behind the health page's history section. Nothing in
      // flight, one finished run — the state a settled archive is in.
      return json(route, {
        home: '/tmp/browser-archive',
        current: null,
        recent: [
          {
            kind: 'reindex',
            status: 'ok',
            at: healthNow,
            duration_s: 12.5,
            phases: [
              { name: 'truth', done: 3, total: 3, elapsed_s: 12.5, rate_per_s: 0.24, eta_s: null },
            ],
          },
        ],
      })
    }
    if (path === '/api/disk') {
      // The storage section's walk of the home. Sized so the four segments are
      // all visibly present rather than one fill and three slivers.
      return json(route, {
        home: '/tmp/browser-archive',
        total_bytes: 32 * 1024 ** 3,
        files: 41189,
        kinds: {
          truth: 8 * 1024 ** 3,
          index: 12 * 1024 ** 3,
          sources: 2 * 1024 ** 3,
          other: 10 * 1024 ** 3,
        },
        rebuildable_bytes: 12 * 1024 ** 3,
        entries: [
          { name: 'index.db', bytes: 11 * 1024 ** 3, kind: 'index' },
          { name: 'truth', bytes: 8 * 1024 ** 3, kind: 'truth' },
          { name: 'pre-ulid-backup', bytes: 6 * 1024 ** 3, kind: 'other' },
          { name: 'source-mirror', bytes: 2 * 1024 ** 3, kind: 'sources' },
        ],
        external: [],
      })
    }
    if (path === '/api/upload') {
      // The write endpoint, answered as the server does: the name the drop
      // landed under and the export it was recognized as.
      return json(route, {
        name: url.searchParams.get('name') ?? 'export.zip',
        kind: 'grok',
        label: 'xAI (Grok)',
        bytes: 4,
        dumps_dir: '/tmp/browser-archive/dumps',
      })
    }
    if (path === '/api/drops') {
      // The drop zone the import page reads: one bundle in each state, so the
      // page's three lists all render rather than every one falling to "empty".
      return json(route, {
        dumps_dir: '/tmp/browser-archive/dumps',
        waiting: [{ name: 'chatgpt-export.zip', bytes: 2048, at: now }],
        imported: [{ name: 'claude-export.zip', bytes: 4096, at: now, kind: 'claude' }],
        failed: [{ name: 'unrecognized.zip', bytes: 64, at: now }],
      })
    }
    if (path === '/api/sources') return json(route, { sources: [{ source: 'claude-code', threads: 1 }] })
    if (path === '/api/thread-types') return json(route, { types: [{ thread_type: 'conversation', threads: 1 }] })
    if (path === '/api/threads') {
      return json(route, {
        threads: [threadListItem],
        total: 1,
        page: 1,
        page_size: Number(url.searchParams.get('limit') ?? 150),
        pages: 1,
      })
    }
    if (path === '/api/search') {
      const query = url.searchParams.get('q') ?? ''
      return json(route, {
        query,
        browse: !query,
        quality: query ? { verdict: 'strong', note: null, n_terms: 1 } : null,
        subjects: [],
        hits: [
          {
            event_id: 12,
            thread_id: THREAD_ID,
            thread_title: threadListItem.title,
            content_type: 'text',
            snippet: query ? 'The needle lives here.' : '',
            full_content: 'The needle lives here.',
            occurred_at: now,
            term_hits: query ? 1 : undefined,
            thread_source: 'claude-code',
            n_events: 3,
          },
        ],
        // The page's position in the match set, as the real endpoint states it.
        total: 1,
        total_threads: 1,
        capped: false,
        exhaustive: true,
        page: Number(url.searchParams.get('page') ?? 1),
        pages: 1,
        page_size: Number(url.searchParams.get('limit') ?? 40),
      })
    }
    if (path === `/api/thread/${THREAD_ID}`) {
      const thinking = url.searchParams.get('thinking') === '1'
      return json(route, {
        thread_id: THREAD_ID,
        title: threadListItem.title,
        source: 'claude-code',
        source_id: 'browser-fixture',
        started_at: now,
        ended_at: now,
        event_count: 3,
        messages: [
          {
            role: 'user',
            blocks: [{ type: 'text', text: 'Where is the needle?' }],
            event_ids: [11],
            meta: { ts: now },
          },
          {
            role: 'assistant',
            blocks: [
              ...(thinking ? [{ type: 'thinking', text: 'Private browser-test reasoning.' }] : []),
              { type: 'tool_use', name: 'Read', input: { file_path: '/tmp/needle.txt' } },
              { type: 'tool_result', output: 'tool fixture complete', truncated: false },
              { type: 'text', text: '**The needle lives here.**' },
            ],
            event_ids: [12],
            meta: { ts: now, models: [MODEL], requests: 1 },
          },
        ],
      })
    }
    if (path === '/api/stats') return json(route, stats)
    if (path === `/api/stats/model/${MODEL}`) return json(route, modelStats)
    if (path === '/api/retrieval') return json(route, retrieval)
    if (path === '/api/search-lab') return json(route, searchLab)
    if (path === '/api/search-lab/runs') return json(route, searchLabRuns)
    if (/^\/api\/search-lab\/runs\/[^/]+\/queries$/.test(path))
      return json(route, searchLabQueries)

    unhandled.push(`${route.request().method()} ${path}`)
    return json(route, { error: 'unhandled browser-test API request' }, 501)
  })

  return unhandled
}

/** Collect failures the DOM assertions alone would otherwise miss. */
export function monitorPage(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(`page: ${error.message}`))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`)
  })
  return errors
}
