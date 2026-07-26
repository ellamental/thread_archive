import { expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ArchiveEntry, Status } from '../api'
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

function loadedArchive(): ArchiveEntry {
  return {
    id: 'aaa111',
    home: '/Users/test/.thread/archive',
    label: 'archive',
    first_seen: now(),
    last_opened: now(),
    exists: true,
    active: true,
    index_bytes: 12_000_000_000,
    load: {},
    runs: [
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

function renderHealth(body: unknown, archives: ArchiveEntry[] = [loadedArchive()]) {
  mswJson('/api/status', body)
  mswJson('/api/archives', { archives })
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
  mswJson('/api/archives', { archives: [] })
  const pending = render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(screen.getByText('checking capture and recovery evidence…')).toBeInTheDocument()
  pending.unmount()

  mswError('/api/status', 503, 'offline')
  mswJson('/api/archives', { archives: [] })
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(await screen.findByText(/health unavailable: 503: offline/)).toBeInTheDocument()
})

it('shows an archive that is loading right now, with phase progress and an ETA', async () => {
  const loading: ArchiveEntry = {
    ...loadedArchive(),
    id: 'bbb222',
    home: '/Users/test/.cache/swe-chat',
    label: 'swe-chat',
    active: false,
    runs: [],
    load: {
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
  renderHealth(healthyStatus(), [loading, loadedArchive()])

  expect(await screen.findByRole('heading', { name: 'Archives on this machine' })).toBeInTheDocument()
  expect(screen.getByText('Loading')).toBeInTheDocument()
  expect(screen.getByText('loading now')).toBeInTheDocument()
  // The live phase, its progress, and the ETA — the numbers a wait is judged by.
  expect(screen.getByText(/6,137 \/ 22,134/)).toBeInTheDocument()
  expect(screen.getByText(/ETA 10.4 min/)).toBeInTheDocument()
  const bar = screen.getByRole('progressbar', { name: 'embed progress' })
  expect(bar).toHaveAttribute('aria-valuenow', '28')
})

it('separates a loaded archive from one that has never recorded a load', async () => {
  const untracked: ArchiveEntry = {
    ...loadedArchive(),
    id: 'ccc333',
    home: '/Users/test/.thread/other',
    label: 'other',
    active: false,
    runs: [],
    load: {},
  }
  renderHealth(healthyStatus(), [loadedArchive(), untracked])

  expect(await screen.findByText('Indexed')).toBeInTheDocument()
  // An index with no tracked load is 'Untracked', not 'Indexed' — unproven, not proven.
  expect(screen.getByText('Untracked')).toBeInTheDocument()
  expect(
    screen.getByText('No tracked load has been recorded for this archive.'),
  ).toBeInTheDocument()
})

it('never lets an indexed archive read as a reachable one', async () => {
  // The two axes are independent: swe-chat can be fully indexed and still answer
  // no query, because retrieval binds to the one home its process was started on.
  const unreachable: ArchiveEntry = {
    ...loadedArchive(),
    id: 'ddd444',
    home: '/Users/test/.cache/swe-chat',
    label: 'swe-chat',
    active: false,
  }
  renderHealth(healthyStatus(), [loadedArchive(), unreachable])

  // Both are Indexed…
  expect(await screen.findAllByText('Indexed')).toHaveLength(2)
  const section = within(screen.getByRole('region', { name: 'Archives on this machine' }))
  // …but exactly one is served, and the other says so out loud rather than
  // leaving reachability to be inferred from a missing badge.
  expect(section.getAllByTitle(/Retrieval answers from this archive/)).toHaveLength(1)
  expect(section.getAllByTitle(/Indexed but not reachable/)).toHaveLength(1)
  expect(
    section.getByText(/answer from the single archive their process was started against/),
  ).toBeInTheDocument()
})

it('shows an archive role as a badge, and no badge when none is set', async () => {
  // The role says what an archive is *for* (live / benchmark / snapshot) —
  // orthogonal to reachability, rendered only when the operator set one.
  const bench: ArchiveEntry = {
    ...loadedArchive(),
    id: 'ddd444',
    home: '/Users/test/.cache/swe-chat',
    label: 'swe-chat',
    active: false,
    role: 'benchmark',
  }
  renderHealth(healthyStatus(), [loadedArchive(), bench])

  const section = within(await screen.findByRole('region', { name: 'Archives on this machine' }))
  expect(section.getByText('benchmark')).toBeInTheDocument()
  // Exactly one role badge: the role-less archive renders none.
  expect(section.getAllByTitle(/What this archive is for/)).toHaveLength(1)
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
  const stalled: ArchiveEntry = {
    ...loadedArchive(),
    runs: [],
    load: { kind: 'embed', status: 'stalled', pid: 999, elapsed_s: 40, phases: [] },
  }
  renderHealth(healthyStatus(), [stalled])

  expect(await screen.findByText('Stalled')).toBeInTheDocument()
  expect(screen.queryByText('loading now')).not.toBeInTheDocument()
})

it('keeps the trust verdict when the archive registry is unreadable', async () => {
  mswJson('/api/status', healthyStatus())
  mswError('/api/archives', 500, 'registry gone')
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )

  // The protection verdict still renders — a registry failure degrades its own
  // section instead of blanking the page.
  expect(await screen.findByRole('heading', { name: 'Your archive is protected' })).toBeInTheDocument()
  const section = screen.getByRole('region', { name: 'Archives on this machine' })
  expect(within(section).getByText('reading the archive registry…')).toBeInTheDocument()
})
