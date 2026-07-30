import { expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { App } from '../App'
import { mswJson, recordRequests } from './msw'


it('renders the real application shell and landing route', async () => {
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  mswJson('/api/status', {
    threads: 0,
    events: 0,
    topics: 0,
    fts_indexed: 0,
    vectors_indexed: 0,
    home: '/tmp/archive',
  })

  render(
    <MemoryRouter initialEntries={['/']}>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getAllByPlaceholderText('search conversations…')).toHaveLength(2)
  expect(screen.getByRole('heading', { name: 'Find the conversation you remember.' })).toBeInTheDocument()
  expect(document.querySelector('.appbar')).toHaveTextContent('Archive/Home')
  expect(document.querySelector('.appbar')).not.toHaveTextContent('events')
})

it('focuses the primary search surface with the slash shortcut', async () => {
  const user = userEvent.setup()
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  render(
    <MemoryRouter initialEntries={['/']}>
      <App />
    </MemoryRouter>,
  )

  await user.keyboard('/')
  expect(document.querySelector('[data-global-search][data-primary="true"]')).toHaveFocus()
})

it('opens and closes the responsive navigation drawer', async () => {
  const user = userEvent.setup()
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  mswJson('/api/status', {
    threads: 0, events: 0, topics: 0, fts_indexed: 0, vectors_indexed: 0, home: '/tmp/archive',
  })
  render(
    <MemoryRouter initialEntries={['/']}>
      <App />
    </MemoryRouter>,
  )
  const navigation = document.querySelector('#archive-navigation')
  expect(navigation).not.toHaveClass('open')
  await user.click(screen.getByRole('button', { name: 'open navigation' }))
  expect(navigation).toHaveClass('open')
  await user.click(screen.getAllByRole('button', { name: 'close navigation' })[0])
  expect(navigation).not.toHaveClass('open')
})

// The dev panels are a different app on a different server (devweb/, port
// 8789). They are not in this bundle and no switch can put them back — so every
// one of their addresses is simply a path this app does not have, and nothing
// here ever calls their endpoints.
it.each(['/retrieval', '/telemetry', '/lab', '/lab/run/abc'])(
  'does not route %s — the dev panels are their own app',
  async (path) => {
    const requested = recordRequests()
    mswJson('/api/threads', { threads: [] })
    mswJson('/api/sources', { sources: [] })
    mswJson('/api/status', {
      threads: 0, events: 0, topics: 0, fts_indexed: 0, vectors_indexed: 0, home: '/tmp/archive',
    })
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    )

    expect(document.querySelector('.content')).toBeEmptyDOMElement()
    // Nothing rendered means nothing asked for their data, either.
    expect(
      requested.some(
        (url) =>
          url.startsWith('/api/search-lab') ||
          url.startsWith('/api/retrieval') ||
          url.startsWith('/api/telemetry'),
      ),
    ).toBe(false)
  },
)
