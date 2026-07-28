// The dev panels: the views whose subject is the machinery rather than the
// archive — the retrieval report (`/retrieval`) and the search lab (`/lab`).
//
// They report on the search *pipeline* and on what the bench has to measure it
// with: a maintainer's instruments, of no use to someone who came here to read
// their conversations. So a viewer does not have them unless its operator says
// so — not merely unadvertised, unrouted: App mounts their routes only when this
// returns true, and until then those addresses are as unknown as any other.
//
// The switch is a line in the archive's config.json (`"dev_panels": true`, which
// `thread-archive web dev` writes and `web --no-dev` clears). The server stamps
// it onto every shell it serves as the meta tag read below, so the answer is in
// the document before the first render — a fetch would decide the route table a
// paint too late and flash a page the viewer does not have. `npm run dev` stamps
// the same tag (see vite.config.ts): a source tree always has the panels.

const META = 'thread-archive-dev-panels'

/** Whether this viewer shows the dev panels. */
export function devPanels(doc: Document = document): boolean {
  const content = doc.querySelector(`meta[name="${META}"]`)?.getAttribute('content')
  if (content == null) return false
  const value = content.trim().toLowerCase()
  return value !== '' && value !== '0' && value !== 'false'
}
