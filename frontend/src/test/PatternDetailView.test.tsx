import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { PatternDetailView } from '../components/PatternDetailView'
import { mswJson } from './msw'

const pattern = {
  id: 'pattern-1', abstraction: 'detail' as const,
  activities: ['tool:error:Bash', 'tool:success:Bash'], length: 2,
  support: 51, support_ratio: 0.5, occurrences: 70, direct_occurrences: 30,
  lift: 4.2, interestingness: 9, examples: [],
}

function page(offset = 0, has_more = true) {
  return {
    status: 'ready', generated_at: '2026-07-22T12:00:00Z', pattern,
    vocabulary: {
      'tool:error:Bash': { id: 'tool:error:Bash', kind: 'tool_error', detail: 'Bash', label: 'error · Bash' },
      'tool:success:Bash': { id: 'tool:success:Bash', kind: 'tool_success', detail: 'Bash', label: 'success · Bash' },
    },
    total: 51, offset, limit: 50, has_more,
    matches: [{
      thread_id: `T${offset + 1}`, title: 'Newest matching thread', source: 'codex',
      event_id: 91, event_ids: [91, 94], matched_at: '2026-07-22T11:00:00Z',
      thread_updated_at: '2026-07-22T11:00:00Z',
    }],
  }
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/experiments/patterns/pattern-1']}>
      <Routes>
        <Route path="/experiments/patterns/:patternId" element={<PatternDetailView />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('PatternDetailView', () => {
  it('lists exact matching threads newest-first with deep links', async () => {
    mswJson('/api/experiments/patterns/pattern-1/matches', page())
    renderDetail()
    expect(await screen.findByText(/Newest first/)).toBeInTheDocument()
    expect(screen.getByText('error · Bash')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Newest matching thread/ })).toHaveAttribute(
      'href', '/archive/T1?e=91',
    )
    expect(screen.getByText('events 91 → 94')).toBeInTheDocument()
  })

  it('pages toward older matches', async () => {
    const user = userEvent.setup()
    mswJson('/api/experiments/patterns/pattern-1/matches', page())
    renderDetail()
    const older = await screen.findByRole('button', { name: 'Older →' })
    mswJson('/api/experiments/patterns/pattern-1/matches', page(50, false))
    await user.click(older)
    expect(await screen.findByText(/showing 51–51/)).toBeInTheDocument()
  })
})
