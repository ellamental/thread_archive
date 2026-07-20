// The curation page's availability seam: the page reports the optional
// thread-librarian package's drains, so it must read as "not installed" when
// /api/curation says the package is absent — and render the drain cards when
// the survey is real.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CurationView } from '../components/CurationView'
import { mswJson } from './msw'

function renderView() {
  return render(
    <MemoryRouter>
      <CurationView />
    </MemoryRouter>,
  )
}

const drain = {
  backlog: 2,
  batch: 10,
  model: 'claude-opus',
  effort: null,
  cadence: { kind: 'interval', interval_s: 3600 },
  heartbeat_at: '2026-07-19T10:00:00Z',
  heartbeat_age_s: 120,
}

const curation = {
  generated_at: '2026-07-19T10:02:00Z',
  days: 30,
  drains: { librarian: drain, gardener: { ...drain, backlog: 0 } },
  graph: {
    topics: 12, in_hierarchy: 10, singletons: 1, uncited: 0,
    unparented: 1, dupe_pairs: 0, hierarchy_pct: 83,
  },
  coverage: {
    conversations: 100, summarized: 90, cited: 80,
    topics_live: 12, topics_archived: 3, citations: 400, links: 40,
  },
  uncuratable: { threads: 0, sample: [] },
  activity: [{ day: '2026-07-18', citations: 5, links: 1, topics: 0 }],
  runs: {
    by_day: [{ day: '2026-07-18', librarian: 2, gardener: 1, requests: 30, output_tokens: 5000 }],
    recent: [],
  },
}

describe('CurationView', () => {
  it('says curation is not installed when the optional package is absent', async () => {
    mswJson('/api/curation', { available: false, error: 'thread-librarian is not installed' })
    renderView()
    expect(await screen.findByText(/curation is not installed/)).toBeInTheDocument()
    expect(screen.queryByText('librarian')).not.toBeInTheDocument()
  })

  it('renders the drain cards from a real survey', async () => {
    mswJson('/api/curation', curation)
    renderView()
    expect(await screen.findByText('librarian')).toBeInTheDocument()
    expect(screen.getByText('gardener')).toBeInTheDocument()
    expect(screen.getByText('2 queued')).toBeInTheDocument()
    expect(screen.getByText('drained')).toBeInTheDocument()
  })

  it('surfaces a failed fetch instead of a blank page', async () => {
    mswJson('/api/curation', 'boom', 500)
    renderView()
    expect(await screen.findByText(/curation stats unavailable/)).toBeInTheDocument()
  })
})
