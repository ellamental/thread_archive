import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import type { TelemetryReport } from '../api'
import { TelemetryView } from '../components/TelemetryView'
import { mswError, mswJson, recordRequests } from './msw'

const sample: TelemetryReport = {
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
        method: 'GET',
        path: '/api/search-lab',
        n: 3,
        errors: 0,
        bytes: 20_000,
        concurrent: 2,
        p50: 320,
        p95: 930,
        p99: 930,
        max: 930,
      },
      {
        method: 'GET',
        path: '/api/status',
        n: 39,
        errors: 2,
        bytes: 80_000,
        concurrent: 5,
        p50: 10,
        p95: 30,
        p99: 40,
        max: 40,
      },
    ],
  },
  ingest: {
    hours: 24,
    sources: {
      codex: {
        passes: 12,
        items: 12,
        events: 88,
        lines: 400,
        bytes: 250_000,
        errors: 1,
        pass_p50_ms: 14,
        pass_p95_ms: 35,
        total_s: 0.2,
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
    {
      file: 'web-requests.jsonl',
      label: 'web requests',
      view: 'telemetry',
      bytes: 4_000_000,
      segments: 1,
      recording: true,
    },
    {
      file: 'retrieval-usage.jsonl',
      label: 'retrieval calls',
      view: 'retrieval',
      bytes: 450_000,
      segments: 2,
      recording: true,
    },
  ],
  recording: true,
}

function view() {
  return render(
    <MemoryRouter>
      <TelemetryView />
    </MemoryRouter>,
  )
}

it('renders endpoint latency, ingest work, faults, and retained ledgers together', async () => {
  mswJson('/api/telemetry', sample)
  view()

  expect(await screen.findByRole('heading', { name: 'Telemetry' })).toBeInTheDocument()
  expect(screen.getByText('/api/search-lab')).toBeInTheDocument()
  expect(screen.getByText('88 events written')).toBeInTheDocument()
  expect(screen.getByText('write_ms')).toBeInTheDocument()
  expect(screen.getByText('codex: could not parse <path>')).toBeInTheDocument()
  expect(screen.getByText('100+')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'retrieval →' })).toHaveAttribute(
    'href',
    '/retrieval',
  )
})

it('requests another retained-history window', async () => {
  const requests = recordRequests()
  mswJson('/api/telemetry', sample)
  view()
  await screen.findByRole('heading', { name: 'Telemetry' })
  await userEvent.selectOptions(screen.getByLabelText('window'), '168')
  await waitFor(() => expect(requests).toContain('/api/telemetry?hours=168'))
})

it('keeps empty ledgers legible instead of hiding their sections', async () => {
  mswJson('/api/telemetry', {
    ...sample,
    web: { ...sample.web, requests: 0, endpoints: [] },
    ingest: { ...sample.ingest, sources: {}, stages: {} },
    faults: [],
  })
  view()
  expect(await screen.findByText('No web requests recorded in this window.')).toBeInTheDocument()
  expect(screen.getByText('No ingest work recorded in this window.')).toBeInTheDocument()
  expect(screen.getByText('No ingest faults retained.')).toBeInTheDocument()
})

it('says when the install writes nothing down, rather than reading as a quiet box', async () => {
  // An install without dev_mode records no runtime telemetry, so an empty page
  // means "not written down" and not "nothing happened" — opposite fixes.
  mswJson('/api/telemetry', {
    ...sample,
    recording: false,
    ledgers: sample.ledgers.map((ledger) => ({ ...ledger, recording: false })),
  })
  view()
  expect(await screen.findByText(/not recording its own runtime/)).toBeInTheDocument()
  // And per ledger, in the inventory that lists them.
  expect(screen.getAllByText('no')).toHaveLength(sample.ledgers.length)
})

it('surfaces a failed fetch', async () => {
  mswError('/api/telemetry', 500, 'broken ledger')
  view()
  expect(await screen.findByText(/Could not load telemetry/)).toBeInTheDocument()
})
