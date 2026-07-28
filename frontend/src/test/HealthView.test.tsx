import { expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { MemoryRouter } from 'react-router-dom'
import type { LoadStatus, Notice, NoticeBoard, Status } from '../api'
import { HealthView } from '../components/HealthView'
import { mswError, mswHandler, mswJson, mswPending } from './msw'

const now = () => new Date().toISOString()

function healthyStatus(): Status {
  const at = now()
  return {
    home: '/Users/test/.thread/archive',
    truth_dir: '/Users/test/.thread/archive/truth',
    index_path: '/Users/test/.thread/archive/index.db',
    threads: 12,
    events: 345,
    topics: 2,
    links: 4,
    fts_indexed: 345,
    vectors_indexed: 300,
    last_checkpoint_at: at,
    last_verify: { at, ok: true, deep: true, hashes: true, parse_errors: 0 },
    last_backup: {
      at,
      ok: true,
      dest: '/Volumes/backup/thread-archive',
      files_copied: 8,
      mirror_complete: true,
    },
    last_restore_drill: { at, ok: true, dest: '/Volumes/backup/thread-archive', events: 345, seconds: 12 },
    last_nightly: { at, ok: true, dest: '/Volumes/backup/thread-archive', failed_stages: [] },
    last_watch_errors: null,
    last_watch_pass: {
      at,
      pid: 42,
      passes: 9,
      sources: {
        'claude-code': { checked: 5, items: 2, events: 20, lines: 30, parse_errors: 0, errors: 0 },
        codex: { checked: 2, items: 1, events: 4, lines: 6, parse_errors: 0, errors: 0 },
      },
    },
    last_coverage: { at, ok: true, sources_checked: 2, failed: [], warnings: [], skips_recent: 0, drift_recent: 0 },
    last_source_mirror: { at, ok: true, copied: 3, files: 12, bytes_out: 4096, errors: 0 },
    last_self_update: { at, ok: true, action: 'up-to-date', current: '0.9.1', reason: 'newest tag is installed' },
    pipeline: {
      ran: true,
      ok: true,
      failed_stages: [],
      recovered_stages: [],
      tolerated_stages: [],
      nightly_at: at,
      dest: '/Volumes/backup/thread-archive',
    },
    watch_process_alive: true,
    backup_same_device: false,
    libraries: [
      {
        name: 'leidenalg + python-igraph',
        tier: 'extra',
        capability: 'Community detection for the search coherence re-rank',
        installed: true,
        state: 'ok',
        detail: 'Leiden partitions the corpus graph.',
      },
      {
        name: 'sentence-transformers + torch',
        tier: 'extra',
        capability: 'Semantic search (the vector arm)',
        installed: false,
        state: 'off',
        detail: 'Search is lexical-only. Install the [embeddings] extra to add the vector arm.',
      },
    ],
  }
}

function loadedRuns(): LoadStatus {
  return {
    home: '/Users/test/.thread/archive',
    current: null,
    recent: [
      {
        kind: 'embed',
        status: 'ok',
        at: now(),
        duration_s: 1215.7,
        phases: [
          {
            name: 'embed',
            done: 22134,
            total: 22134,
            elapsed_s: 1215.7,
            rate_per_s: 18.2,
            eta_s: null,
            detail_s: { select: 0.1, encode: 1210.7, write: 4 },
          },
        ],
      },
    ],
  }
}

const noLoads: LoadStatus = { home: '/Users/test/.thread/archive', current: null, recent: [] }

/** A home whose bytes are mostly *not* its conversations — the case the storage
 *  section exists for. */
function diskUsage(overrides: Record<string, unknown> = {}) {
  return {
    home: '/home/u/.thread/archive',
    total_bytes: 32 * 1024 ** 3,
    files: 41_189,
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
    ...overrides,
  }
}

/** The action queue is built by the server (`_ops/notices.py`), so the page's
 *  job is rendering it and driving its two writes — which is what these stub. */
function notice(overrides: Partial<Notice> = {}): Notice {
  return {
    key: 'same-disk',
    tone: 'warn',
    title: 'Backup is on the same filesystem as the archive',
    detail: 'This protects against index corruption, but not loss of the disk.',
    command: 'thread-archive daemon install --backup --dest /Volumes/disk/thread-archive',
    fingerprint: 'abc123',
    ...overrides,
  }
}

const noNotices: NoticeBoard = { active: [], silenced: [] }

function renderHealth(
  body: unknown,
  loads: LoadStatus = loadedRuns(),
  board: NoticeBoard = noNotices,
) {
  mswJson('/api/status', body)
  mswJson('/api/loads', loads)
  mswJson('/api/disk', diskUsage())
  mswJson('/api/notices', board)
  return render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
}

/** The queue plus a live silence store behind it: the two writes move notices
 *  between the lists and answer with the board, exactly as the router does. */
function renderQueue(board: NoticeBoard) {
  const state: NoticeBoard = { active: [...board.active], silenced: [...board.silenced] }
  const move = (key: string, silence: boolean) => {
    const from = silence ? state.active : state.silenced
    const found = from.findIndex((n) => n.key === key)
    if (found < 0) return HttpResponse.text(`no active notice with key '${key}'`, { status: 404 })
    const [moved] = from.splice(found, 1)
    ;(silence ? state.silenced : state.active).push(
      silence ? { ...moved, silenced_at: new Date().toISOString() } : { ...moved, silenced_at: null },
    )
    return HttpResponse.json(state)
  }
  mswJson('/api/status', healthyStatus())
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
  mswHandler(
    http.get('/api/notices', () => HttpResponse.json(state)),
    http.post('/api/notices/silence', ({ request }) =>
      move(new URL(request.url).searchParams.get('key') || '', true)),
    http.post('/api/notices/unsilence', ({ request }) =>
      move(new URL(request.url).searchParams.get('key') || '', false)),
  )
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  return state
}

it('turns operational evidence into a clear protected verdict', async () => {
  renderHealth(healthyStatus())

  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  expect(screen.getByText(/a backup can actually restore/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Capture' })).toBeInTheDocument()
  expect(screen.getByText('Always-on capture is active with 9 completed passes.')).toBeInTheDocument()
  expect(screen.getByText('First-class')).toBeInTheDocument()
  expect(screen.getByText('Best effort')).toBeInTheDocument()
  expect(screen.getByText('/Volumes/backup/thread-archive')).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Action queue' })).not.toBeInTheDocument()
})

it('reports search libraries, and a feature merely absent raises no action', async () => {
  renderHealth(healthyStatus())

  expect(await screen.findByRole('heading', { name: 'Search libraries' })).toBeInTheDocument()
  expect(screen.getByText('leidenalg + python-igraph')).toBeInTheDocument()
  // An uninstalled extra is reported, never as a fault: a lexical-only install is a
  // shape of the product, not a broken one, so it still reads as protected.
  expect(screen.getByText('Not installed')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Action queue' })).not.toBeInTheDocument()
})

it('reports a degraded library in the table even when nothing is queued', async () => {
  const status = healthyStatus()
  status.libraries[0] = {
    ...status.libraries[0],
    installed: false,
    state: 'degraded',
    detail: 'The coherence re-rank is running on Louvain.',
  }
  renderHealth(status)

  expect(await screen.findByRole('heading', { name: 'Search libraries' })).toBeInTheDocument()
  expect(screen.getByText('Degraded')).toBeInTheDocument()
})

it('prioritizes unresolved protection gaps and gives executable remedies', async () => {
  renderHealth(healthyStatus(), loadedRuns(), {
    active: [
      notice({
        key: 'nightly-failed',
        tone: 'bad',
        title: 'Protection failed at backup',
        detail: 'The pipeline verdict accounts for later successful reruns.',
        command: 'thread-archive nightly /Volumes/backup/thread-archive',
      }),
      notice(),
      notice({
        key: 'update',
        tone: 'good',
        title: 'v0.9.2 is available',
        detail: 'past soak window',
        command: 'thread-archive self-update',
      }),
    ],
    silenced: [],
  })

  // The hero reads the queue: one protection gap outranks two lesser notices.
  expect(await screen.findByRole('heading', { name: 'Protection is incomplete' })).toBeInTheDocument()
  expect(screen.getByText(/cannot be relied on yet/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '1 protection gap' })).toBeInTheDocument()
  expect(screen.getByText('Protection failed at backup')).toBeInTheDocument()
  expect(screen.getByText('Backup is on the same filesystem as the archive')).toBeInTheDocument()
  expect(screen.getByText('v0.9.2 is available')).toBeInTheDocument()
  expect(screen.getByText('thread-archive nightly /Volumes/backup/thread-archive')).toBeInTheDocument()
  expect(screen.getByText('thread-archive self-update')).toBeInTheDocument()
})

it('does not promise a working restore while the queue holds a warning', async () => {
  renderHealth(healthyStatus(), loadedRuns(), { active: [notice()], silenced: [] })

  expect(await screen.findByRole('heading', { name: 'Your archive needs attention' })).toBeInTheDocument()
  expect(screen.getByText(/clear them to keep a restore trustworthy/)).toBeInTheDocument()
  expect(screen.queryByText(/a backup can actually restore/)).not.toBeInTheDocument()
})

it('does not hide a provider parser failure inside an otherwise fresh pass', async () => {
  const status = healthyStatus()
  status.last_watch_pass!.sources!.codex.parse_errors = 3
  renderHealth(status, loadedRuns(), {
    active: [notice({
      key: 'source-codex',
      tone: 'bad',
      title: 'codex is not importing cleanly',
      detail: '3 parse errors and 0 watcher errors since this capture process started.',
      command: 'thread-archive fix-import codex',
    })],
    silenced: [],
  })

  expect(await screen.findByText('codex is not importing cleanly')).toBeInTheDocument()
  expect(screen.getByText('thread-archive fix-import codex')).toBeInTheDocument()
  // The provider table degrades on its own evidence, not on the notice.
  expect(screen.getByText('Degraded')).toBeInTheDocument()
})

// ---- silencing -------------------------------------------------------------
it('silences a notice and keeps it one click from being read again', async () => {
  const user = userEvent.setup()
  renderQueue({ active: [notice()], silenced: [] })
  await screen.findByRole('heading', { name: '1 warning' })

  await user.click(screen.getByRole('button', { name: 'Silence' }))

  // Out of the queue, but never out of sight: the count is the whole honesty of
  // silencing, and the notice is readable in full behind it.
  expect(await screen.findByRole('heading', { name: 'Nothing needs attention' })).toBeInTheDocument()
  expect(screen.queryByText('Backup is on the same filesystem as the archive')).not.toBeInTheDocument()
  const toggle = screen.getByRole('button', { name: '1 silenced' })
  expect(toggle).toHaveAttribute('aria-expanded', 'false')

  await user.click(toggle)
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText('Backup is on the same filesystem as the archive')).toBeInTheDocument()
  expect(screen.getByText(/silenced just now/)).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Unsilence' }))

  expect(await screen.findByRole('heading', { name: '1 warning' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '1 silenced' })).not.toBeInTheDocument()
})

it('leaves the rest of the queue alone when one notice is silenced', async () => {
  const user = userEvent.setup()
  renderQueue({
    active: [
      notice(),
      notice({ key: 'update-blocked', title: 'Updates are blocked', detail: 'tree not clean' }),
    ],
    silenced: [],
  })
  await screen.findByRole('heading', { name: '2 warnings' })

  await user.click(screen.getAllByRole('button', { name: 'Silence' })[0])

  expect(await screen.findByRole('heading', { name: '1 warning' })).toBeInTheDocument()
  expect(screen.getByText('Updates are blocked')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '1 silenced' })).toBeInTheDocument()
})

it('says so when a silence does not reach the archive', async () => {
  const user = userEvent.setup()
  mswJson('/api/status', healthyStatus())
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
  mswJson('/api/notices', { active: [notice()], silenced: [] })
  mswHandler(
    http.post('/api/notices/silence', () => HttpResponse.text('read-only home', { status: 500 })),
  )
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  await screen.findByRole('heading', { name: '1 warning' })

  await user.click(screen.getByRole('button', { name: 'Silence' }))

  // A write that failed must not read as a button that does nothing: the notice
  // stays, and the reason is on the page.
  expect(await screen.findByText('read-only home')).toBeInTheDocument()
  expect(screen.getByText('Backup is on the same filesystem as the archive')).toBeInTheDocument()
})

it('keeps the trust verdict when the action queue is unreadable', async () => {
  mswJson('/api/status', healthyStatus())
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
  mswError('/api/notices', 500, 'queue gone')
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )

  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Action queue' })).not.toBeInTheDocument()
})

it('renders durable loading and error states', async () => {
  mswPending('/api/status')
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
  mswJson('/api/notices', noNotices)
  const pending = render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(screen.getByText('checking capture and recovery evidence…')).toBeInTheDocument()
  pending.unmount()

  mswError('/api/status', 503, 'offline')
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
  mswJson('/api/notices', noNotices)
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(await screen.findByText(/health unavailable: 503: offline/)).toBeInTheDocument()
})

it('shows the load in flight, with phase progress and an ETA', async () => {
  const inFlight: LoadStatus = {
    ...loadedRuns(),
    current: {
      kind: 'reindex',
      status: 'running',
      started_at: now(),
      elapsed_s: 90,
      phase: 'embed',
      phases: [
        { name: 'truth', done: 726, total: 726, elapsed_s: 12, rate_per_s: 60, eta_s: null },
        { name: 'embed', done: 6137, total: 22134, elapsed_s: 78, rate_per_s: 25.6, eta_s: 624 },
      ],
    },
  }
  renderHealth(healthyStatus(), inFlight)

  expect(await screen.findByText('reindex in flight')).toBeInTheDocument()
  expect(screen.getByText('Loading')).toBeInTheDocument()
  expect(screen.getByText('loading now')).toBeInTheDocument()
  // The live phase, its progress, and the ETA — the numbers a wait is judged by.
  expect(screen.getByText(/6,137 \/ 22,134/)).toBeInTheDocument()
  expect(screen.getByText(/ETA 10.4 min/)).toBeInTheDocument()
  const bar = screen.getByRole('progressbar', { name: 'embed progress' })
  expect(bar).toHaveAttribute('aria-valuenow', '28')
})

it('reports what past loads cost, per phase', async () => {
  renderHealth(healthyStatus())

  expect(await screen.findByRole('heading', { name: 'What past loads cost' })).toBeInTheDocument()
  const history = within(screen.getByRole('region', { name: 'What past loads cost' }))
  // The run's total, its phase breakdown, and its verdict all land in the row.
  expect(history.getAllByText('20.3 min').length).toBeGreaterThan(0)
  // 'embed' twice: the load's kind, and the phase chip inside it.
  expect(history.getAllByText('embed')).toHaveLength(2)
  expect(history.getByText('Complete')).toBeInTheDocument()
})

it('marks a load whose process died as stalled rather than running', async () => {
  const stalled: LoadStatus = {
    ...noLoads,
    current: { kind: 'embed', status: 'stalled', pid: 999, elapsed_s: 40,
               phases: [{ name: 'embed', done: 10, total: 100, elapsed_s: 40,
                          rate_per_s: 0.25, eta_s: null }] },
  }
  renderHealth(healthyStatus(), stalled)

  expect(await screen.findByText('Stalled')).toBeInTheDocument()
  expect(
    screen.getByText(/The process writing this state is gone/),
  ).toBeInTheDocument()
  expect(screen.queryByText('loading now')).not.toBeInTheDocument()
})

it('keeps the trust verdict when the load ledger is unreadable', async () => {
  mswJson('/api/status', healthyStatus())
  mswError('/api/loads', 500, 'ledger gone')
  mswJson('/api/disk', diskUsage())
  mswJson('/api/notices', noNotices)
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )

  // The protection verdict still renders — a ledger failure degrades its own
  // section instead of blanking the page.
  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  const section = screen.getByRole('region', { name: 'What past loads cost' })
  expect(within(section).getByText(/No loads recorded yet/)).toBeInTheDocument()
})

it('breaks the disk total into what must be kept and what rebuilds', async () => {
  renderHealth(healthyStatus())
  await screen.findByRole('heading', { name: 'Your archive is protected' })

  const section = screen.getByRole('region', { name: 'What this archive costs on disk' })
  expect(await within(section).findByText('32.0 GB · 41,189 files')).toBeInTheDocument()
  // Every kind is named and sized — the split is the point, not the total.
  // 'Truth' reads twice on purpose: once in the legend, once as an entry's kind.
  expect(within(section).getAllByText('Truth')).toHaveLength(2)
  expect(within(section).getByText('8.0 GB · 25%')).toBeInTheDocument()
  expect(within(section).getByText('12.0 GB · 38%')).toBeInTheDocument()
  // The entries behind it, so an unlabelled 10 GB becomes a name to act on.
  expect(within(section).getByText('pre-ulid-backup')).toBeInTheDocument()
  expect(within(section).getByText(/12.0 GB of this is index/)).toBeInTheDocument()
})

it('identifies disk segments by label, never by colour alone', async () => {
  renderHealth(healthyStatus())
  await screen.findByRole('heading', { name: 'Your archive is protected' })

  const section = screen.getByRole('region', { name: 'What this archive costs on disk' })
  const meter = await within(section).findByRole('img')
  // The bar carries the whole reading for anyone who cannot see the fills.
  expect(meter).toHaveAccessibleName(
    'Truth 8.0 GB, Index 12.0 GB, Raw sources 2.0 GB, Other 10.0 GB',
  )
})

it('measures storage on its own cadence and survives its failure', async () => {
  mswJson('/api/status', healthyStatus())
  mswJson('/api/loads', noLoads)
  mswError('/api/disk', 500, 'walk failed')
  mswJson('/api/notices', noNotices)
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )

  // A failed walk degrades its own section; the rest of the evidence stands.
  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  const section = screen.getByRole('region', { name: 'What this archive costs on disk' })
  expect(within(section).getByText('measuring the archive home…')).toBeInTheDocument()
})
