import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { PatternsView } from '../components/PatternsView'
import type { PatternReport } from '../api'
import { mswError, mswJson, mswPending } from './msw'

function renderPatterns() {
  return render(
    <MemoryRouter initialEntries={['/experiments/patterns']}>
      <PatternsView />
    </MemoryRouter>,
  )
}

function report(overrides: Partial<PatternReport> = {}): PatternReport {
  return {
    version: 1,
    status: 'ready',
    report_path: '/tmp/archive/experiments/patterns/report.json',
    generated_at: '2026-07-22T12:00:00Z',
    through_event_id: 99,
    stale: false,
    stale_events: 0,
    config: { thread_types: ['conversation'], min_support: 2, max_length: 3, max_gap: 2, max_patterns: 20 },
    corpus: { threads: 4, events: 200, sequence_events: 30, thread_types: { conversation: 4 } },
    vocabulary: {
      shape: {
        'tool:error': { id: 'tool:error', kind: 'tool_error', detail: null, label: 'tool error' },
        'tool:call': { id: 'tool:call', kind: 'tool_call', detail: null, label: 'tool call' },
      },
      detail: {
        'tool:error:Bash': { id: 'tool:error:Bash', kind: 'tool_error', detail: 'Bash', label: 'error · Bash' },
        'tool:call:Bash': { id: 'tool:call:Bash', kind: 'tool_call', detail: 'Bash', label: 'call · Bash' },
      },
    },
    patterns: [
      {
        id: 'p-detail', abstraction: 'detail', activities: ['tool:error:Bash', 'tool:call:Bash'],
        length: 2, support: 3, support_ratio: 0.75, occurrences: 5,
        direct_occurrences: 2, lift: 2.5, interestingness: 4.2,
        examples: [{ thread_id: 'T1', title: 'A recovery trace', source: 'fixture', event_id: 7, event_ids: [7, 9] }],
      },
      {
        id: 'p-shape', abstraction: 'shape', activities: ['tool:error', 'tool:call'],
        length: 2, support: 4, support_ratio: 1, occurrences: 6,
        direct_occurrences: 6, lift: 1.2, interestingness: 1.1, examples: [],
      },
    ],
    ...overrides,
  }
}

describe('PatternsView', () => {
  it('shows loading and fetch errors', async () => {
    mswPending('/api/experiments/patterns')
    const first = renderPatterns()
    expect(screen.getByText('loading mined patterns…')).toBeInTheDocument()
    first.unmount()

    mswError('/api/experiments/patterns', 500, 'boom')
    renderPatterns()
    expect(await screen.findByText(/patterns unavailable: 500: boom/)).toBeInTheDocument()
  })

  it('explains how to produce the first report', async () => {
    mswJson('/api/experiments/patterns', report({ status: 'not_run', patterns: [], corpus: undefined, vocabulary: undefined }))
    renderPatterns()
    expect(await screen.findByText('No pattern report has been mined yet.')).toBeInTheDocument()
    expect(screen.getByText('thread_archive patterns')).toBeInTheDocument()
  })

  it('renders metrics, sequences, and deep-linked examples', async () => {
    mswJson('/api/experiments/patterns', report())
    renderPatterns()
    expect(await screen.findByRole('heading', { name: 'Patterns' })).toBeInTheDocument()
    expect(screen.getByText('error · Bash')).toBeInTheDocument()
    expect(screen.getByText('call · Bash')).toBeInTheDocument()
    expect(screen.getByText('2.50×')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'A recovery trace' })).toHaveAttribute(
      'href', '/archive/T1?e=7',
    )
    expect(screen.getByRole('link', { name: /View 3 matching threads/ })).toHaveAttribute(
      'href', '/experiments/patterns/p-detail',
    )
  })

  it('filters by lens and activity text', async () => {
    const user = userEvent.setup()
    mswJson('/api/experiments/patterns', report())
    renderPatterns()
    await screen.findByText('error · Bash')
    await user.selectOptions(screen.getByLabelText('pattern lens'), 'shape')
    expect(screen.queryByText('error · Bash')).not.toBeInTheDocument()
    expect(screen.getByText('tool error')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('filter activities or tools…'), 'nothing')
    expect(screen.getByText('no patterns match these filters')).toBeInTheDocument()
  })

  it('hides newer events until the report is over 24 hours old', async () => {
    const generated_at = new Date(Date.now() - 23 * 60 * 60 * 1000).toISOString()
    mswJson('/api/experiments/patterns', report({ generated_at, stale: true, stale_events: 17 }))
    renderPatterns()
    expect(await screen.findByRole('heading', { name: 'Patterns' })).toBeInTheDocument()
    expect(screen.queryByText(/17 newer events are not represented/)).not.toBeInTheDocument()
  })

  it('marks a report stale once it is over 24 hours old', async () => {
    const generated_at = new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString()
    mswJson('/api/experiments/patterns', report({ generated_at, stale: true, stale_events: 17 }))
    renderPatterns()
    expect(await screen.findByText(/17 newer events are not represented/)).toBeInTheDocument()
  })
})
