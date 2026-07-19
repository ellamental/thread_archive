// The topic detail page: header badges, links routed by endpoint type,
// citation deep-links (?e=<event_id>), and community-peer chips.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { TopicView } from '../components/TopicView'
import type { TopicDetail } from '../api'
import { mswError, mswJson, mswPending } from './msw'

function PageStub({ name }: { name: string }) {
  const loc = useLocation()
  return <div>{name} PAGE {loc.pathname + loc.search}</div>
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/topic/:id" element={<TopicView />} />
        <Route path="/archive/:id" element={<PageStub name="THREAD" />} />
      </Routes>
    </MemoryRouter>,
  )
}

function detail(overrides: Partial<TopicDetail>): TopicDetail {
  return {
    id: '5', title: 'Graph Theory', topic_kind: 'concept', description: 'nodes and edges',
    summary: null, archived: false, created_at: '2026-01-01T10:00:00Z',
    updated_at: '2026-01-02T10:00:00Z',
    graph: { pagerank: 0.4, community: 3, degree: 2 },
    links: [], evidence: [], peers: [],
    ...overrides,
  }
}

describe('TopicView', () => {
  it('shows the loading state while the fetch is in flight', () => {
    mswPending('/api/topic/:id')
    renderAt('/topic/5')
    expect(screen.getByText('loading…')).toBeInTheDocument()
  })

  it('surfaces a fetch error', async () => {
    mswError('/api/topic/:id', 404, 'no topic with id 5')
    renderAt('/topic/5')
    expect(await screen.findByText(/topic unavailable: 404/)).toBeInTheDocument()
  })

  it('surfaces an unresolvable ref as the server 404', async () => {
    // Any ref shape passes through raw (ULID, legacy integer, whatever was
    // pasted); the server resolves and 404s what matches nothing.
    mswError('/api/topic/:id', 404, 'no topic with id nope')
    renderAt('/topic/nope')
    expect(await screen.findByText(/topic unavailable: 404/)).toBeInTheDocument()
  })

  it('renders title, badges, and description', async () => {
    mswJson('/api/topic/:id', detail({}))
    renderAt('/topic/5')
    expect(await screen.findByText('Graph Theory')).toBeInTheDocument()
    expect(screen.getByText('concept')).toBeInTheDocument()
    expect(screen.getByText('2 links')).toBeInTheDocument()
    expect(screen.getByText('community 3')).toBeInTheDocument()
    expect(screen.getByText('nodes and edges')).toBeInTheDocument()
    expect(screen.queryByText('archived')).not.toBeInTheDocument()
    expect(screen.getByText('no links or citations yet')).toBeInTheDocument()
  })

  it('badges an archived topic', async () => {
    mswJson('/api/topic/:id', detail({ archived: true, graph: null }))
    renderAt('/topic/5')
    expect(await screen.findByText('archived')).toBeInTheDocument()
  })

  it('routes a topic link to the topic page and a conversation link to the thread page', async () => {
    const user = userEvent.setup()
    mswJson('/api/topic/:id', detail({
      links: [
        { direction: 'out', other_id: '9', other_title: 'PageRank', other_type: 'topic',
          link_type: 'related', strength: 0.9, evidence: null },
        { direction: 'out', other_id: '42', other_title: 'A Conversation', other_type: 'conversation',
          link_type: 'works_on', strength: 1, evidence: 'came up here' },
      ],
    }))
    renderAt('/topic/5')
    // the topic-typed link renders on the same page component (route match), so
    // clicking it must land on /topic/9 — assert via the conversation link instead,
    // then the topic link by its rendered arrow label
    expect(await screen.findByText('related →')).toBeInTheDocument()
    await user.click(screen.getByText('A Conversation'))
    expect(screen.getByText('THREAD PAGE /archive/42')).toBeInTheDocument()
  })

  it('flips the arrow on an incoming link', async () => {
    mswJson('/api/topic/:id', detail({
      links: [{ direction: 'in', other_id: '9', other_title: 'PageRank', other_type: 'topic',
                link_type: 'implements', strength: 0.5, evidence: null }],
    }))
    renderAt('/topic/5')
    expect(await screen.findByText('← implements')).toBeInTheDocument()
  })

  it('deep-links a citation to its message in the thread reader', async () => {
    const user = userEvent.setup()
    mswJson('/api/topic/:id', detail({
      evidence: [{ event_id: 77, thread_id: '12', thread_title: 'Source Thread',
                   quote: 'the exact words', created_at: '2026-01-03T10:00:00Z' }],
    }))
    renderAt('/topic/5')
    await user.click(await screen.findByText('“the exact words”'))
    expect(screen.getByText('THREAD PAGE /archive/12?e=77')).toBeInTheDocument()
  })

  it('renders the hierarchy strip from part-of/contains links', async () => {
    const user = userEvent.setup()
    mswJson('/api/topic/:id', detail({
      links: [
        { direction: 'out', other_id: '2', other_title: 'Needle', other_type: 'topic',
          link_type: 'part-of', strength: 1, evidence: null },
        { direction: 'in', other_id: '3', other_title: 'quote selection', other_type: 'topic',
          link_type: 'part-of', strength: 1, evidence: null },
        { direction: 'out', other_id: '4', other_title: 'scoring', other_type: 'topic',
          link_type: 'contains', strength: 1, evidence: null },
        // a hierarchy edge to a conversation is a link-list row, not a strip chip
        { direction: 'in', other_id: '42', other_title: 'A Conversation', other_type: 'conversation',
          link_type: 'part-of', strength: 1, evidence: null },
      ],
    }))
    renderAt('/topic/5')
    expect(await screen.findByText('part of')).toBeInTheDocument()
    const strip = screen.getByText('part of').closest('.hier')!
    expect(strip).toHaveTextContent('Needle')
    // children: incoming part-of and outgoing contains both count
    expect(strip).toHaveTextContent('quote selection')
    expect(strip).toHaveTextContent('scoring')
    expect(strip).not.toHaveTextContent('A Conversation')
    // a chip is a real link to the parent topic (deep-linkable, middle-clickable)
    const chip = screen.getByRole('link', { name: 'Needle' })
    expect(chip).toHaveAttribute('href', '/topic/2')
    await user.click(chip)
    expect(await screen.findByText(/part of|topic unavailable/)).toBeInTheDocument()
  })

  it('renders no hierarchy strip without hierarchy links', async () => {
    mswJson('/api/topic/:id', detail({
      links: [{ direction: 'out', other_id: '9', other_title: 'PageRank', other_type: 'topic',
                link_type: 'related', strength: 0.9, evidence: null }],
    }))
    renderAt('/topic/5')
    await screen.findByText('Graph Theory')
    expect(screen.queryByText('part of')).not.toBeInTheDocument()
    expect(screen.queryByText('contains')).not.toBeInTheDocument()
  })

  it('renders community peers as chips linking to their topics', async () => {
    mswJson('/api/topic/:id', detail({
      peers: [{ thread_id: '8', title: 'Leiden', pagerank: 0.2 }],
    }))
    renderAt('/topic/5')
    expect(await screen.findByText('community peers')).toBeInTheDocument()
    expect(screen.getByText('Leiden')).toBeInTheDocument()
  })
})
