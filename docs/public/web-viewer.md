# Web viewer

**The viewer is dev-only — it runs from a source checkout and ships in no wheel.**
`thread_archive._web` and its built bundle are excluded from the wheel, because a
browser UI is not what an install is for: preservation, retrieval, and the MCP
server are, and the bundle alone is a quarter of the download.
`thread_archive._viewer.viewer_available()` is the
probe, and everything that would offer the viewer asks it first — so
`thread-archive web` and `watch --web` are registered only where the viewer
exists, `setup` offers a browser only there, and the service layer writes no
unit carrying `--web` without one. An install neither advertises nor half-serves
a UI it hasn't got.

In a checkout, the always-on watcher cohosts a local search + reader UI:
`thread-archive watch --web` (the watcher service passes it) serves at
`http://127.0.0.1:8787` — a stdlib HTTP server handing out a pre-built React
bundle plus a few JSON endpoints, in the watcher's *own* process. One process,
one SQLite engine — the viewer reads concurrently with the watcher's writes,
which WAL makes safe (`_store/_base.py`). No second daemon: the viewer exists
where the persistent URL is.

`thread-archive web` opens that URL in a browser. An opener, not a server.
`thread-archive web dev` puts a link to the dev panels in the rail first (below);
`web --no-dev` takes it back out.

**The URLs are steady within a checkout.** Other programs link into the viewer —
editor "open in archive" buttons, sibling consoles' navbars, health probes — so
these paths keep working (a promise to this machine's own family rather than a
public interface, since no install has them — see
[stability.md](stability.md)):

| Path | What it is |
| --- | --- |
| `/` | landing: recent threads, global search |
| `/search` | search results (`?q=` plus filters; `?page=` walks them) |
| `/threads` | every thread (`?page=` walks them) |
| `/stats` | token/cost analytics (`/stats/model/<model>` drills in) |
| `/health` | the archive's own status page |
| `/upload` | import an account export: drop the ZIP, and where to get one |
| `/docs` | the manual: every page this installation carries |
| `/docs/<slug>` | one manual page, rendered |
| `/archive/<thread_id>` | one conversation, rendered |
| `GET /api/health` | `{ok, home}` — cheap liveness for probes |
| `GET /api/archive-link?id=<session-uuid>` | resolve a provider session id to its thread (below) |

Every page route has a real-browser case in `frontend/e2e/` — one Chromium
navigation per route, asserting its landmark renders with no console errors and
no unmocked fetch — and `route-coverage.spec.ts` keeps that a bijection, so a
new route without a browser case reds the suite.

**The manual reads here too.** `/docs` lists this directory and `/docs/<slug>`
renders one page, served as markdown by `/api/docs` and rendered in the browser
by the renderer transcripts already use — so the documentation is at the same
address as the thing it documents. The pages come from
`thread_archive._docs`, the same resolver behind `thread-archive docs`, which
reads the packaged copy where there is one and this checkout's `docs/public/`
where there isn't: editing a page here lands on the next request with nothing to
rebuild. `docs/*.md` is not served — the maintainer's pages are one directory
up, which is the whole public/internal split. Links between pages are routes; a
link out of the manual (`../devweb.md`, `../../SECURITY.md`) leaves for the
repository, since this server serves the manual and not the tree around it.

**The maintainer's instruments are not here.** `/retrieval` (how search is
performing), `/telemetry` (the operational ledgers read together) and `/lab`
(what the bench has to measure with) are a separate app on a separate server —
`devweb/`, run with `python -m devweb`, on `127.0.0.1:8789`. See
[../devweb.md](../devweb.md). This server routes none of them and serves none of their
endpoints: `/api/retrieval`, `/api/telemetry` and `/api/search-lab*` answer `404`
here, and their page addresses fall through to a shell whose app has no route for
them.

What this viewer *can* do is point at them, and it does that only when asked:

```json
{ "dev_panels": true }
```

one line in the home's `config.json`, which `thread-archive web dev` writes and
`web --no-dev` clears. The server stamps that answer onto every shell it serves
and the rail grows a `dev panels ↗` link to `http://127.0.0.1:8789` — an absolute
href, since it leaves this origin. Without the line the rail simply does not name
them, which for someone who came here to read their conversations is one less
unexplained word. It is read per request: flipping the line lands on the next
page load, with nothing to restart. The switch moves a link and nothing else —
it cannot start that server, and it cannot bring the pages back here.

Everything under `/api/` other than the two named above backs the viewer's own
bundle and is private — it changes with the frontend. So is the markup: the interface is
the URL, not the DOM. No page view touches the conversation record; the stats
pages do fold a derived rollup into the index, which is the rebuildable
projection. The server binds loopback only, since it serves the whole archive
with no auth (a non-loopback bind needs `THREAD_ARCHIVE_WEB_NONLOCAL=1`).

**One endpoint writes**: `POST /api/upload` takes an account-export ZIP and puts
it in the drop zone the cohosting watcher already imports from, which is what
`/upload` is a page for — the import itself stays the watcher's, with the settle
and retain/quarantine rules it already owns. Reading is what a `GET` can do; every
other method on every other path is a `405`. A write carries two guards past the
Host check every request passes: a loopback `Origin`, and an `X-Archive-Write`
header — which no cross-origin form can set, so a page this server did not serve
must first win a preflight that is never answered.

**Runtime is node-free**: the bundle is built ahead of time and committed under
`_web/static/`, so running the viewer from a checkout never touches node (and an
install, having no viewer, never touches it either). Node is a *build*-only tool:

```bash
# rebuild the bundle after editing the frontend (node only here):
cd frontend && npm install && npm run build   # → ../src/thread_archive/_web/static/
```

**Archive-links.** With that persistent server, the archive owns the editor
"open this conversation" link itself: `GET /api/archive-link?id=<session-uuid>&source=claude-code`
resolves the session to its thread via `ImportState` and returns `{thread_id, url, id}`
(the `id` echoing which candidate resolved),
or `&redirect=1` → a `302` to `/archive/<id>`. (Local — no separate backend
involved.) `id` may repeat — a caller that cannot tell which uuid it holds is the
session id sends every candidate, best guess first, and the first that resolves
wins; ids that were never imported are skipped, not fatal.
