import { expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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
