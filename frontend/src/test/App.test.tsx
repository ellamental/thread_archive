import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from '../App'
import { mswJson } from './msw'


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

  expect(screen.getByPlaceholderText('search conversations…')).toBeInTheDocument()
  expect(screen.getByText(/Search above/)).toBeInTheDocument()
  await waitFor(() => {
    expect(document.querySelector('.statusbar')).toHaveTextContent('0 threads · 0 events')
  })
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
