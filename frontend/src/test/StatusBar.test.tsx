import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { StatusBar } from '../components/StatusBar'

function renderAt(path: string, onSearch = vi.fn()) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <StatusBar onSearch={onSearch} />
    </MemoryRouter>,
  )
  return onSearch
}

describe('app header', () => {
  it('names the current section without corpus telemetry', () => {
    renderAt('/threads')
    expect(screen.getByText('Browse')).toBeInTheDocument()
    expect(screen.queryByText(/indexed|events|vectors/)).not.toBeInTheDocument()
  })

  it('labels a thread as a conversation', () => {
    renderAt('/archive/01ARZ3NDEKTSV4RRFFQ69G5FAV')
    expect(screen.getByText('Conversation')).toBeInTheDocument()
  })

  it('labels the trust center as health', () => {
    renderAt('/health')
    expect(screen.getByText('Health')).toBeInTheDocument()
  })

  it('offers a global search shortcut', async () => {
    const user = userEvent.setup()
    const onSearch = renderAt('/stats')
    await user.click(screen.getByRole('button', { name: /search/i }))
    expect(onSearch).toHaveBeenCalledOnce()
  })
})
