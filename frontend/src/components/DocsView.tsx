import { isValidElement, useEffect, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import type { Components } from 'react-markdown'
import { api, type Doc, type DocPage } from '../api'
import { Markdown } from './Markdown'

// The manual, in the viewer: the same pages the package ships and
// `thread-archive docs` prints, served as markdown by /api/docs and rendered
// here by the renderer transcripts already use. Reading the docs beside the
// conversations means the answer to "how does this thing work" is at the same
// address as the thing.

// Where a link that leaves the manual goes. The docs cross-link a few files
// outside docs/public/ (../../SECURITY.md, ../devweb.md); those exist in the
// repo, not in this server's manual, so they resolve upstream rather than 404
// into the SPA shell. Same repo the package metadata points at.
const REPO_BLOB = 'https://github.com/ellamental/thread_archive/blob/main/'

/** Where the manual sits in the repo — what a page's relative links resolve against. */
const DOCS_ROOT = 'docs/public'

/** A page-relative path as a repo path: `../devweb.md` → `docs/devweb.md`. */
function repoPath(path: string): string {
  const out: string[] = []
  for (const segment of `${DOCS_ROOT}/${path}`.split('/')) {
    if (segment === '..') out.pop()
    else if (segment && segment !== '.') out.push(segment)
  }
  return out.join('/')
}

/** The visible text under a node — what a heading's anchor id is built from. */
function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children)
  return ''
}

/**
 * A heading's anchor id, GitHub-style: lowercase, punctuation dropped, spaces
 * hyphenated. The docs already link their own sections that way
 * (`format.md#extension-region`), so matching that spelling is what makes those
 * links land — here and on the repo page both.
 */
export function headingId(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\- ]+/g, '')
    .trim()
    .replace(/ +/g, '-')
}

/**
 * Resolve one link out of a manual page.
 *
 * `stability.md` and `stability.md#anchor` are how the docs address each other,
 * and here those are routes — internal, so a click stays in the app and keeps
 * the rail. A bare `#anchor` stays a same-page jump. Everything else leaves:
 * absolute URLs as written, and any other relative path (`../../SECURITY.md`)
 * to the repo, resolved from where the manual sits in it, since this server
 * serves the manual and not the tree around it.
 */
export function resolveDocHref(href: string, slugs: Set<string>): { to: string; external: boolean } {
  if (href.startsWith('#')) return { to: href, external: false }
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) return { to: href, external: true }
  const [path, hash] = href.split('#')
  const slug = path.replace(/\.md$/, '')
  if (slugs.has(slug)) return { to: `/docs/${slug}` + (hash ? '#' + hash : ''), external: false }
  return { to: REPO_BLOB + repoPath(path), external: true }
}

/** The renderers a manual page needs on top of the shared markdown defaults. */
function docComponents(slugs: Set<string>): Components {
  const heading = (Tag: 'h1' | 'h2' | 'h3' | 'h4') =>
    function Heading({ node: _node, children, ...props }: { node?: unknown; children?: ReactNode }) {
      return (
        <Tag id={headingId(textOf(children))} {...props}>
          {children}
        </Tag>
      )
    }
  return {
    h1: heading('h1'),
    h2: heading('h2'),
    h3: heading('h3'),
    h4: heading('h4'),
    a({ node: _node, href, children, ...props }) {
      if (!href) return <span>{children}</span>
      const { to, external } = resolveDocHref(href, slugs)
      if (external) {
        return (
          <a href={to} target="_blank" rel="noreferrer noopener" {...props}>
            {children}
          </a>
        )
      }
      if (to.startsWith('#')) {
        return (
          <a href={to} {...props}>
            {children}
          </a>
        )
      }
      return <Link to={to}>{children}</Link>
    },
  }
}

/** The index: every page the install carries, in the manual's own reading order. */
export function DocsView() {
  const [pages, setPages] = useState<DocPage[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .docs()
      .then((p) => live && setPages(p))
      .catch((e: Error) => live && setErr(e.message))
    return () => {
      live = false
    }
  }, [])

  if (err) return <div className="empty">the manual is unavailable: {err}</div>
  if (!pages) return <div className="empty">loading…</div>

  return (
    <div className="wrap docs-page">
      <h1>The manual</h1>
      <p className="docs-lede">
        thread-archive's own documentation, shipped inside the package — the same pages{' '}
        <code>thread-archive docs</code> prints at a terminal.
      </p>
      {pages.length === 0 ? (
        <div className="empty">this installation carries no manual</div>
      ) : (
        <div className="docs-index">
          {pages.map((page) => (
            <Link key={page.slug} className="docs-card" to={'/docs/' + page.slug}>
              <span className="docs-card-title">{page.title}</span>
              <span className="docs-card-slug">{page.slug}</span>
              {page.summary && <span className="docs-card-summary">{page.summary}</span>}
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}

/** One page, rendered. Its own `# heading` is the page title — nothing restates it. */
export function DocView() {
  const { slug } = useParams()
  const [doc, setDoc] = useState<Doc | null>(null)
  const [slugs, setSlugs] = useState<Set<string>>(new Set())
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setDoc(null)
    setErr(null)
    if (!slug) return
    api
      .doc(slug)
      .then((d) => live && setDoc(d))
      .catch((e: Error) => live && setErr(e.message))
    // The index too, and not for display: which links in this page are pages
    // here and which leave is exactly the set of slugs this install carries.
    api
      .docs()
      .then((pages) => live && setSlugs(new Set(pages.map((p) => p.slug))))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [slug])

  // Client-side navigation does not act on a fragment, so an anchor link
  // between pages would land at the top of the new one. Scroll it in once the
  // text is up.
  useEffect(() => {
    if (!doc || !window.location.hash) return
    document.getElementById(window.location.hash.slice(1))?.scrollIntoView()
  }, [doc])

  return (
    <div className="wrap docs-page doc-page">
      <div className="doc-crumb">
        <Link to="/docs">‹ the manual</Link>
        {doc && <span className="doc-crumb-slug">{doc.slug}.md</span>}
      </div>
      {err && <div className="empty">no such page: {err}</div>}
      {!err && !doc && <div className="empty">loading…</div>}
      {doc && (
        <Markdown className="md-doc" components={docComponents(slugs)}>
          {doc.markdown}
        </Markdown>
      )}
    </div>
  )
}
