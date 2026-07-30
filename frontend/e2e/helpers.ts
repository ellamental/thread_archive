import type { Page, Route } from '@playwright/test'

export const THREAD_ID = '01ARZ3NDEKTSV4RRFFQ69G5FAV'
export const MODEL = 'claude-opus-4-8'
/** The manual page the browser suite opens, and the index it is listed in. */
export const DOC_SLUG = 'cli'
export const DOC_PAGES = [
  { slug: DOC_SLUG, title: 'CLI', summary: 'One namespaced command.' },
  { slug: 'install', title: 'Install', summary: 'Python ≥ 3.12, macOS or Linux.' },
]
/** The recorded benchmark run the browser suite opens — the reported pass of a
 *  row still on the bench, which is the case carrying every section of the page. */

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
    // The manual. Two pages: enough for the index to be a list, and the second
    // is what the first page's cross-link resolves to.
    if (path === '/api/docs') return json(route, { pages: DOC_PAGES })
    if (path === `/api/docs/${DOC_SLUG}`) {
      return json(route, {
        slug: DOC_SLUG,
        title: 'CLI',
        markdown:
          '# CLI\n\nOne namespaced command. See [install.md](install.md).\n\n' +
          '```bash\nthread-archive status\n```\n',
      })
    }
    if (path === '/api/docs/install') {
      return json(route, { slug: 'install', title: 'Install', markdown: '# Install\n\nPython ≥ 3.12.\n' })
    }

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
