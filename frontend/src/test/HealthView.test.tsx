import { expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { LoadStatus, Status } from '../api'
import { HealthView } from '../components/HealthView'
import { mswError, mswJson, mswPending } from './msw'

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

function renderHealth(body: unknown, loads: LoadStatus = loadedRuns()) {
  mswJson('/api/status', body)
  mswJson('/api/loads', loads)
  mswJson('/api/disk', diskUsage())
  return render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
}

it('turns operational evidence into a clear protected verdict', async () => {
  renderHealth(healthyStatus())

  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
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

it('raises the silent fault when a live feature lost the library that does it well', async () => {
  const status = healthyStatus()
  status.libraries[0] = {
    ...status.libraries[0],
    installed: false,
    state: 'degraded',
    detail: 'The coherence re-rank is running on Louvain.',
  }
  renderHealth(status)

  expect(await screen.findByRole('heading', { name: 'Your archive needs attention' })).toBeInTheDocument()
  expect(screen.getByText('leidenalg + python-igraph is not installed')).toBeInTheDocument()
  expect(screen.getByText("pip install 'thread-archive[all]'")).toBeInTheDocument()
  expect(screen.getByText('Degraded')).toBeInTheDocument()
})

it('prioritizes unresolved protection gaps and gives executable remedies', async () => {
  const status = healthyStatus()
  status.pipeline.ok = false
  status.pipeline.failed_stages = ['backup']
  status.last_nightly!.ok = false
  status.last_nightly!.failed_stages = ['backup']
  status.last_coverage!.warnings = ['ChatGPT export is 12 days old']
  status.backup_same_device = true
  status.last_self_update = {
    at: now(),
    ok: true,
    action: 'update',
    current: '0.9.1',
    tag: 'v0.9.2',
    reason: 'past soak window',
  }
  renderHealth(status)

  expect(await screen.findByRole('heading', { name: 'Protection is incomplete' })).toBeInTheDocument()
  expect(screen.getByText('Protection failed at backup')).toBeInTheDocument()
  expect(screen.getByText('Backup is on the same filesystem as the archive')).toBeInTheDocument()
  expect(screen.getByText('ChatGPT export is 12 days old')).toBeInTheDocument()
  expect(screen.getByText('v0.9.2 is available')).toBeInTheDocument()
  expect(screen.getByText('thread_archive nightly /Volumes/backup/thread-archive')).toBeInTheDocument()
  expect(screen.getByText('thread_archive self-update')).toBeInTheDocument()
})

it('does not hide a provider parser failure inside an otherwise fresh pass', async () => {
  const status = healthyStatus()
  status.last_watch_pass!.sources!.codex.parse_errors = 3
  renderHealth(status)

  expect(await screen.findByText('codex is not importing cleanly')).toBeInTheDocument()
  expect(screen.getByText('thread_archive fix-import codex')).toBeInTheDocument()
  expect(screen.getByText('Degraded')).toBeInTheDocument()
})

it('renders durable loading and error states', async () => {
  mswPending('/api/status')
  mswJson('/api/loads', noLoads)
  mswJson('/api/disk', diskUsage())
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
