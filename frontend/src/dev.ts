// Dev pages: the views whose subject is the machinery rather than the archive.
//
// The retrieval page reports on the search *pipeline* — served latency by warm
// and cold regime, per-stage costs, gold-run quality. That is the maintainer's
// instrument, not something a person who came here to read their conversations
// has any use for, so it does not sit in the navigation by default. It is not
// hidden, either: the route always resolves, because a local single-user viewer
// hiding a page from the only person who can reach it would be theatre. The gate
// is over *prominence* — whether the rail advertises it.
//
// Turned on by visiting with `?dev=1` (`thread-archive web dev` opens exactly
// that), off with `?dev=0`, and remembered in localStorage so the choice
// survives navigation and reloads rather than living in every URL.

const KEY = 'thread-archive:dev'

/** Whether dev pages are advertised in this browser.
 *
 *  Reads the URL first so a fresh `?dev=1` takes effect on the load that carries
 *  it, then falls back to what was stored. Storage can throw (private windows,
 *  disabled cookies) and a dev toggle must never be what breaks the viewer, so
 *  every access is guarded and a failure simply means "off".
 */
export function devMode(search = window.location.search): boolean {
  const requested = new URLSearchParams(search).get('dev')
  if (requested !== null) {
    const on = requested !== '0' && requested !== 'false'
    try {
      window.localStorage.setItem(KEY, on ? '1' : '0')
    } catch {
      // Unstorable: honor it for this page load and don't persist.
    }
    return on
  }
  try {
    return window.localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}
