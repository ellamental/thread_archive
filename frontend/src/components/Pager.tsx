// The page walker every paginated list on the viewer uses.
//
// Rendered once above the rows and once below them: a page of results is taller
// than the window, so a control at one end only is a scroll away from wherever
// the reader finished reading. Each copy names its own position, because two
// navigation landmarks with one name are indistinguishable to anything reading
// the page by structure.
//
// First/Last are here rather than only Previous/Next: the ends of a set are
// where a reader actually goes — the oldest thread, the last page of a walk —
// and stepping to page 38 one Next at a time is not a way to get there.
export function Pager({
  page,
  pages,
  label,
  position,
  onGo,
}: {
  page: number
  pages: number
  /** What is being paged ("thread pages", "result pages"): the aria-label stem. */
  label: string
  position: 'top' | 'bottom'
  onGo: (page: number) => void
}) {
  // One page is the whole set, so there is nothing to walk. Nothing to walk with
  // zero pages either — an empty result renders its own "nothing matched" line.
  if (pages <= 1) return null
  return (
    <nav className="pagination" aria-label={`${label} ${position}`}>
      <button className="toolbar-btn" disabled={page === 1} onClick={() => onGo(1)}>
        First
      </button>
      <button className="toolbar-btn" disabled={page === 1} onClick={() => onGo(page - 1)}>
        Previous
      </button>
      <span>
        Page {page.toLocaleString()} of {pages.toLocaleString()}
      </span>
      <button className="toolbar-btn" disabled={page === pages} onClick={() => onGo(page + 1)}>
        Next
      </button>
      <button className="toolbar-btn" disabled={page === pages} onClick={() => onGo(pages)}>
        Last
      </button>
    </nav>
  )
}
