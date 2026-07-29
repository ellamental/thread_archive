import { afterEach, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from '../App'
import { mswError, mswJson, recordRequests } from './msw'

/** Stamp the shell the way the server does for an operator who asked for the
 *  dev panels (`"dev_panels": true` in config.json). */
function stampDevPanels(): void {
  const meta = document.createElement('meta')
  meta.setAttribute('name', 'thread-archive-dev-panels')
  meta.setAttribute('content', '1')
  document.head.appendChild(meta)
}

afterEach(() => {
  document.head
    .querySelectorAll('meta[name="thread-archive-dev-panels"]')
    .forEach((m) => m.remove())
})


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
    <MemoryRouter
      initialEntries={['/']}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
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

// The dev panels ship in the bundle and mount only for a viewer whose operator
// asked for them. Unmounted has to mean *unrouted*, not hidden-but-reachable:
// the address is the whole of what a shipped viewer would otherwise expose.
it('does not route the lab in a viewer that was not asked for the dev panels', async () => {
  const requested = recordRequests()
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  mswJson('/api/status', {
    threads: 0, events: 0, topics: 0, fts_indexed: 0, vectors_indexed: 0, home: '/tmp/archive',
  })
  render(
    <MemoryRouter initialEntries={['/lab']}>
      <App />
    </MemoryRouter>,
  )

  expect(document.querySelector('.content')).toBeEmptyDOMElement()
  // Nothing rendered means nothing asked the lab for data, either.
  expect(requested.some((url) => url.startsWith('/api/search-lab'))).toBe(false)
})

it('routes the lab once the shell says the operator asked for the dev panels', async () => {
  stampDevPanels()
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  mswJson('/api/status', {
    threads: 0, events: 0, topics: 0, fts_indexed: 0, vectors_indexed: 0, home: '/tmp/archive',
  })
  // The page itself is covered by SearchLabView's own tests; this asserts the
  // route mounts, which its error state shows as well as its data would.
  mswError('/api/search-lab', 503, 'bench offline')
  mswError('/api/search-lab/runs', 503, 'bench offline')
  render(
    <MemoryRouter initialEntries={['/lab']}>
      <App />
    </MemoryRouter>,
  )

  expect(await screen.findByRole('heading', { name: 'Search lab', level: 1 })).toBeInTheDocument()
})

it('routes telemetry only when developer panels are enabled', async () => {
  const requested = recordRequests()
  mswJson('/api/threads', { threads: [] })
  mswJson('/api/sources', { sources: [] })
  render(
    <MemoryRouter initialEntries={['/telemetry']}>
      <App />
    </MemoryRouter>,
  )
  expect(document.querySelector('.content')).toBeEmptyDOMElement()
  expect(requested.some((url) => url.startsWith('/api/telemetry'))).toBe(false)

  stampDevPanels()
  mswError('/api/telemetry', 503, 'offline')
  render(
    <MemoryRouter initialEntries={['/telemetry']}>
      <App />
    </MemoryRouter>,
  )
  expect(await screen.findByText(/Could not load telemetry/)).toBeInTheDocument()
})
