// The page walker shared by the browse and search-result lists: which page each
// button asks for, which ones are dead at the ends of a set, and the case where
// there is nothing to walk.
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Pager } from '../components/Pager'

function renderPager(page: number, pages: number) {
  const onGo = vi.fn()
  render(<Pager page={page} pages={pages} label="result pages" position="top" onGo={onGo} />)
  return onGo
}

describe('Pager', () => {
  it('renders nothing when one page holds the whole set', () => {
    renderPager(1, 1)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('renders nothing when there are no pages at all', () => {
    renderPager(1, 0)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('names its own position, so two copies of it are distinguishable', () => {
    render(<Pager page={2} pages={4} label="thread pages" position="bottom" onGo={() => {}} />)
    expect(screen.getByRole('navigation', { name: 'thread pages bottom' })).toBeInTheDocument()
  })

  it('walks to each end and one step either way', async () => {
    const user = userEvent.setup()
    const onGo = renderPager(3, 9)
    expect(screen.getByText('Page 3 of 9')).toBeInTheDocument()

    await user.click(screen.getByText('First'))
    await user.click(screen.getByText('Previous'))
    await user.click(screen.getByText('Next'))
    await user.click(screen.getByText('Last'))
    expect(onGo.mock.calls.map(([n]) => n)).toEqual([1, 2, 4, 9])
  })

  it('has no step back from the first page', async () => {
    const user = userEvent.setup()
    const onGo = renderPager(1, 4)
    expect(screen.getByText('First')).toBeDisabled()
    expect(screen.getByText('Previous')).toBeDisabled()
    await user.click(screen.getByText('Next'))
    expect(onGo).toHaveBeenCalledWith(2)
  })

  it('has no step forward from the last page', async () => {
    const user = userEvent.setup()
    const onGo = renderPager(4, 4)
    expect(screen.getByText('Next')).toBeDisabled()
    expect(screen.getByText('Last')).toBeDisabled()
    await user.click(screen.getByText('Previous'))
    expect(onGo).toHaveBeenCalledWith(3)
  })

  it('spells long page counts out with separators', () => {
    renderPager(1200, 13580)
    expect(screen.getByText('Page 1,200 of 13,580')).toBeInTheDocument()
  })
})
