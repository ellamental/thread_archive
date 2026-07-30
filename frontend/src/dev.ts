// Whether the rail offers a way over to the dev panels.
//
// The panels — the retrieval report, telemetry, the search lab — are a
// different app on a different server (`devweb/`, `python -m devweb`, port
// 8789). Nothing in this bundle can mount or gate them, and this does not try
// to: what it decides is whether this app's navigation names them at all. For
// someone who came here to read their conversations that link is one more
// unexplained word in the rail, so a viewer offers it only when its operator
// says so.
//
// The switch is a line in the archive's config.json (`"dev_panels": true`, which
// `thread-archive web dev` writes and `web --no-dev` clears). The server stamps
// it onto every shell it serves as the meta tag read below, so the answer is in
// the document before the first render — a fetch would grow the link a paint
// late and shift the rail under a cursor already moving. `npm run dev` stamps
// the same tag (see vite.config.ts): a source tree is where the panels are.

const META = 'thread-archive-dev-panels'

/** Where the panels live. Their server's default port — move it with `--port`
 *  and this link goes stale, which is the cost of the viewer not being able to
 *  ask a server it does not run. */
export const DEV_PANELS_URL = 'http://127.0.0.1:8789'

/** Whether this viewer offers the link. */
export function devPanels(doc: Document = document): boolean {
  const content = doc.querySelector(`meta[name="${META}"]`)?.getAttribute('content')
  if (content == null) return false
  const value = content.trim().toLowerCase()
  return value !== '' && value !== '0' && value !== 'false'
}
