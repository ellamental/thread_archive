import { expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Status } from '../api'
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
  }
}

function renderHealth(body: unknown) {
  mswJson('/api/status', body)
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
  const pending = render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(screen.getByText('checking capture and recovery evidence…')).toBeInTheDocument()
  pending.unmount()

  mswError('/api/status', 503, 'offline')
  render(
    <MemoryRouter>
      <HealthView />
    </MemoryRouter>,
  )
  expect(await screen.findByText(/health unavailable: 503: offline/)).toBeInTheDocument()
})
