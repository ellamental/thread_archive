import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { DocsView, DocView, headingId, resolveDocHref } from '../components/DocsView'
import { mswError, mswJson, mswPending } from './msw'

const PAGES = [
  { slug: 'install', title: 'Install', summary: 'Python ≥ 3.12, macOS or Linux.' },
  { slug: 'cli', title: 'CLI', summary: 'One namespaced command.' },
]

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/docs" element={<DocsView />} />
        <Route path="/docs/:slug" element={<DocView />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('the manual index', () => {
  it('lists every page the install carries, in the order served', async () => {
    mswJson('/api/docs', { pages: PAGES })
    renderAt('/docs')

    const links = await screen.findAllByRole('link', { name: /Install|CLI/ })
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/docs/install', '/docs/cli'])
    expect(screen.getByText('Python ≥ 3.12, macOS or Linux.')).toBeInTheDocument()
  })

  it('says so when the install ships no manual', async () => {
    mswJson('/api/docs', { pages: [] })
    renderAt('/docs')
    expect(await screen.findByText('this installation carries no manual')).toBeInTheDocument()
  })

  it('reports an unreachable manual instead of an empty page', async () => {
    mswError('/api/docs', 500, 'boom')
    renderAt('/docs')
    expect(await screen.findByText(/the manual is unavailable/)).toBeInTheDocument()
  })

  it('shows a loading state while the index is in flight', () => {
    mswPending('/api/docs')
    renderAt('/docs')
    expect(screen.getByText('loading…')).toBeInTheDocument()
  })
})

describe('one manual page', () => {
  it('renders the markdown and names the file it came from', async () => {
    mswJson('/api/docs', { pages: PAGES })
    mswJson('/api/docs/cli', {
      slug: 'cli',
      title: 'CLI',
      markdown: '# CLI\n\nOne namespaced command.\n\n```bash\nthread-archive status\n```\n',
    })
    renderAt('/docs/cli')

    expect(await screen.findByRole('heading', { name: 'CLI' })).toBeInTheDocument()
    expect(screen.getByText('One namespaced command.')).toBeInTheDocument()
    expect(screen.getByText('cli.md')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '‹ the manual' })).toHaveAttribute('href', '/docs')
  })

  it('routes a cross-link to another page in the manual, without a page load', async () => {
    const user = userEvent.setup()
    mswJson('/api/docs', { pages: PAGES })
    mswJson('/api/docs/cli', {
      slug: 'cli',
      title: 'CLI',
      markdown: '# CLI\n\nSee [install.md](install.md).\n',
    })
    mswJson('/api/docs/install', { slug: 'install', title: 'Install', markdown: '# Install\n\nPython ≥ 3.12.\n' })
    renderAt('/docs/cli')

    const link = await screen.findByRole('link', { name: 'install.md' })
    expect(link).toHaveAttribute('href', '/docs/install')
    await user.click(link)
    expect(await screen.findByText('Python ≥ 3.12.')).toBeInTheDocument()
  })

  it('sends a link out of the manual to the repository, in a new tab', async () => {
    mswJson('/api/docs', { pages: PAGES })
    mswJson('/api/docs/cli', {
      slug: 'cli',
      title: 'CLI',
      markdown: '# CLI\n\nSee [the policy](../../SECURITY.md).\n',
    })
    renderAt('/docs/cli')

    const link = await screen.findByRole('link', { name: 'the policy' })
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/ellamental/thread_archive/blob/main/SECURITY.md',
    )
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('gives headings the ids the docs anchor-link', async () => {
    mswJson('/api/docs', { pages: PAGES })
    mswJson('/api/docs/format', {
      slug: 'format',
      title: 'Format',
      markdown: '# Format\n\n## The extension region\n\nReserved.\n',
    })
    renderAt('/docs/format')

    expect(await screen.findByRole('heading', { name: 'The extension region' })).toHaveAttribute(
      'id',
      'the-extension-region',
    )
  })

  it('reports a page that is not in this install', async () => {
    mswJson('/api/docs', { pages: PAGES })
    mswError('/api/docs/nope', 404, 'no such manual page')
    renderAt('/docs/nope')
    expect(await screen.findByText(/no such page/)).toBeInTheDocument()
  })
})

describe('link resolution', () => {
  const slugs = new Set(['cli', 'install'])

  it('keeps same-page anchors, routes known pages, exports the rest', () => {
    expect(resolveDocHref('#extension-region', slugs)).toEqual({
      to: '#extension-region',
      external: false,
    })
    expect(resolveDocHref('cli.md', slugs)).toEqual({ to: '/docs/cli', external: false })
    expect(resolveDocHref('cli.md#tree', slugs)).toEqual({ to: '/docs/cli#tree', external: false })
    expect(resolveDocHref('https://example.org/x', slugs)).toEqual({
      to: 'https://example.org/x',
      external: true,
    })
    // A path out of docs/public/, and a docs page this install does not carry:
    // both resolve upstream rather than 404 into the app shell. Relative walks
    // resolve from where the manual sits in the repo, however many levels up.
    expect(resolveDocHref('../../search_lab/beir_eval.py', slugs).to).toBe(
      'https://github.com/ellamental/thread_archive/blob/main/search_lab/beir_eval.py',
    )
    expect(resolveDocHref('../../SECURITY.md', slugs).to).toBe(
      'https://github.com/ellamental/thread_archive/blob/main/SECURITY.md',
    )
    expect(resolveDocHref('gone.md', slugs).to).toBe(
      'https://github.com/ellamental/thread_archive/blob/main/docs/public/gone.md',
    )
    // The manual's internal half is one level up, in the repo and in no install,
    // so a public page linking one sends the reader upstream rather than to a
    // dead route.
    expect(resolveDocHref('../devweb.md', slugs)).toEqual({
      to: 'https://github.com/ellamental/thread_archive/blob/main/docs/devweb.md',
      external: true,
    })
  })

  it('slugs headings the way the docs spell their anchors', () => {
    expect(headingId('The extension region')).toBe('the-extension-region')
    expect(headingId('Why it is separate?')).toBe('why-it-is-separate')
  })
})
