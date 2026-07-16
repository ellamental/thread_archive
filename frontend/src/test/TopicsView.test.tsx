// The topics page: loading / error / empty states, community grouping with the
// unlinked group trailing, client-side filtering, row-click navigation, and the
// communities ↔ hierarchy view switch with its collapsible tree.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { TopicsView } from '../components/TopicsView'
import type { TopicListItem, TopicsResponse, TopicTreeNode } from '../api'
import { mswError, mswJson, mswPending } from './msw'

function TopicStub() {
  const loc = useLocation()
  return <div>TOPIC PAGE {loc.pathname}</div>
}

function renderTopics(url = '/topics') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/topics" element={<TopicsView />} />
        <Route path="/topic/:id" element={<TopicStub />} />
      </Routes>
    </MemoryRouter>,
  )
}

function topic(overrides: Partial<TopicListItem>): TopicListItem {
  return {
    id: 1, title: 'Graph Theory', topic_kind: 'concept', description: 'nodes and edges',
    evidence_count: 2, link_count: 3, community: 0, pagerank: 0.4,
    updated_at: '2026-01-01T10:00:00Z',
    ...overrides,
  }
}

function response(topics: TopicListItem[]): TopicsResponse {
  return {
    topics,
    graph: { available: true, nodes: topics.length, communities: 2, components: 1, community_engine: 'leiden' },
  }
}

describe('TopicsView', () => {
  it('shows the loading state while the fetch is in flight', () => {
    mswPending('/api/topics')
    renderTopics()
    expect(screen.getByText('loading…')).toBeInTheDocument()
  })

  it('surfaces a fetch error', async () => {
    mswError('/api/topics', 500, 'boom')
    renderTopics()
    expect(await screen.findByText(/topics unavailable: 500: boom/)).toBeInTheDocument()
  })

  it('says when no topics are curated yet', async () => {
    mswJson('/api/topics', { topics: [], graph: { available: true, nodes: 0 } })
    renderTopics()
    expect(await screen.findByText('no topics curated yet')).toBeInTheDocument()
  })

  it('groups topics by community, unlinked last, anchored by the top topic', async () => {
    mswJson('/api/topics', response([
      topic({ id: 1, title: 'Graph Theory', community: 0, pagerank: 0.4 }),
      topic({ id: 2, title: 'PageRank', community: 0, pagerank: 0.2 }),
      topic({ id: 3, title: 'Lone Island', community: null, pagerank: 0, link_count: 0, evidence_count: 0 }),
    ]))
    renderTopics()
    // the community group is headed by its highest-pagerank member
    expect(await screen.findByText('community · 2 topics')).toBeInTheDocument()
    expect(screen.getByText('unlinked')).toBeInTheDocument()
    // status line reflects the graph survey
    expect(screen.getByText('3 topics · 2 communities (leiden)')).toBeInTheDocument()
    // group order: community first, unlinked trailing
    const headers = screen.getAllByText(/community ·|no links yet/)
    expect(headers[0]).toHaveTextContent('community · 2 topics')
  })

  it('badges kind, links, and citations on a row', async () => {
    mswJson('/api/topics', response([topic({ link_count: 3, evidence_count: 2 })]))
    renderTopics()
    expect(await screen.findByText('concept')).toBeInTheDocument()
    expect(screen.getByText('3 links')).toBeInTheDocument()
    expect(screen.getByText('2 citations')).toBeInTheDocument()
  })

  it('filters topics by title or description', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics', response([
      topic({ id: 1, title: 'Graph Theory' }),
      topic({ id: 2, title: 'Leiden', description: 'community detection' }),
    ]))
    renderTopics()
    await screen.findAllByText('Graph Theory')
    await user.type(screen.getByPlaceholderText('filter topics…'), 'detection')
    // 'Graph Theory' disappears entirely; 'Leiden' remains (community header + row)
    expect(screen.queryByText('Graph Theory')).not.toBeInTheDocument()
    expect(screen.getAllByText('Leiden').length).toBeGreaterThan(0)
  })

  it('navigates to the topic when a row is clicked', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics', response([topic({ id: 7, title: 'Graph Theory' })]))
    renderTopics()
    // the title shows in the community header too — the row's copy is the last one
    const rows = await screen.findAllByText('Graph Theory')
    await user.click(rows[rows.length - 1])
    expect(screen.getByText('TOPIC PAGE /topic/7')).toBeInTheDocument()
  })
})

function node(overrides: Partial<TopicTreeNode>): TopicTreeNode {
  return { id: 1, title: 'Needle', topic_kind: 'artifact', children: [], ...overrides }
}

describe('TopicsView hierarchy', () => {
  it('switches to the tree on the hierarchy tab and back', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics', response([topic({})]))
    mswJson('/api/topics/tree', { roots: [], topics_in_hierarchy: 0, topics_total: 3 })
    renderTopics()
    await screen.findAllByText('Graph Theory')
    await user.click(screen.getByText('hierarchy'))
    expect(await screen.findByText('no part-of / contains links curated yet')).toBeInTheDocument()
    await user.click(screen.getByText('communities'))
    expect(await screen.findAllByText('Graph Theory')).not.toHaveLength(0)
  })

  it('opens straight into the tree via ?view=tree', async () => {
    mswJson('/api/topics/tree', {
      roots: [node({ id: 1, title: 'Needle', children: [node({ id: 2, title: 'compression' })] })],
      topics_in_hierarchy: 2,
      topics_total: 10,
    })
    renderTopics('/topics?view=tree')
    expect(await screen.findByText('Needle')).toBeInTheDocument()
    expect(screen.getByText('2 of 10 topics in the hierarchy · 1 roots')).toBeInTheDocument()
    // roots start open: the child shows without a click
    expect(screen.getByText('compression')).toBeInTheDocument()
  })

  it('collapses and expands a subtree from the caret', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics/tree', {
      roots: [node({ id: 1, title: 'Needle', children: [node({ id: 2, title: 'compression' })] })],
      topics_in_hierarchy: 2,
      topics_total: 2,
    })
    renderTopics('/topics?view=tree')
    await screen.findByText('compression')
    await user.click(screen.getByLabelText('collapse'))
    expect(screen.queryByText('compression')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('expand'))
    expect(screen.getByText('compression')).toBeInTheDocument()
  })

  it('keeps deeper levels closed until expanded', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics/tree', {
      roots: [node({
        id: 1, title: 'Needle',
        children: [node({ id: 2, title: 'compression', children: [node({ id: 3, title: 're-compression' })] })],
      })],
      topics_in_hierarchy: 3,
      topics_total: 3,
    })
    renderTopics('/topics?view=tree')
    await screen.findByText('compression')
    // depth-1 starts closed: its child is hidden until its caret is clicked
    expect(screen.queryByText('re-compression')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('expand'))
    expect(screen.getByText('re-compression')).toBeInTheDocument()
  })

  it('navigates to the topic from a tree title', async () => {
    const user = userEvent.setup()
    mswJson('/api/topics/tree', {
      roots: [node({ id: 9, title: 'Needle' })],
      topics_in_hierarchy: 1,
      topics_total: 1,
    })
    renderTopics('/topics?view=tree')
    await user.click(await screen.findByText('Needle'))
    expect(screen.getByText('TOPIC PAGE /topic/9')).toBeInTheDocument()
  })

  it('surfaces a tree fetch error', async () => {
    mswError('/api/topics/tree', 500, 'boom')
    renderTopics('/topics?view=tree')
    expect(await screen.findByText(/hierarchy unavailable: 500: boom/)).toBeInTheDocument()
  })
})
