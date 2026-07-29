# Web viewer

The always-on watcher cohosts a local search + reader UI: `thread-archive watch --web`
(the shipped watcher service passes it) serves at `http://127.0.0.1:8787` — a stdlib
HTTP server handing out a pre-built React bundle plus a few JSON endpoints, in
the watcher's *own* process. One process, one SQLite engine — the viewer reads
concurrently with the watcher's writes, which WAL makes safe (`_store/_base.py`).
No second daemon: the viewer exists where the persistent URL is.

`thread-archive web` opens that URL in a browser. An opener, not a server.
`thread-archive web dev` turns on the dev panels (below) and opens it.

**The URLs are a supported interface.** Other programs link into the viewer —
editor "open in archive" buttons, sibling consoles' navbars, health probes — so
these paths keep working:

| Path | What it is |
| --- | --- |
| `/` | landing: recent threads, global search |
| `/search` | search results (`?q=` plus filters; `?page=` walks them) |
| `/threads` | every thread (`?page=` walks them) |
| `/stats` | token/cost analytics (`/stats/model/<model>` drills in) |
| `/health` | the archive's own status page |
| `/upload` | import an account export: drop the ZIP, and where to get one |
| `/archive/<thread_id>` | one conversation, rendered |
| `GET /api/health` | `{ok, home}` — cheap liveness for probes |
| `GET /api/archive-link?id=<session-uuid>` | resolve a provider session id to its thread (below) |

Every page route has a real-browser case in `frontend/e2e/` — one Chromium
navigation per route, asserting its landmark renders with no console errors and
no unmocked fetch — and `route-coverage.spec.ts` keeps that a bijection, so a
new route without a browser case reds the suite.

**Dev panels** are the exception to the table above, and deliberately not part of
its contract. `/retrieval` reports on the search *pipeline* — served latency by
warm and cold regime, per-stage costs — and `/lab` reports on what the bench has
to measure it with. Both are maintainer's instruments rather than anything the
archive is for, so **a viewer does not have them unless its operator asks**: one
line in the home's `config.json`,

```json
{ "dev_panels": true }
```

which `thread-archive web dev` writes and `web --no-dev` clears. The server
stamps that answer onto every shell it serves and the app mounts their routes
only when it is there, so without the line those addresses route nowhere — not
hidden behind an unadvertised link, absent. It is read per request: flipping the
line lands on the next page load, with nothing to restart. Their data comes from
`search_lab/`, which lives in the source repo and not in an install, so a `pip
install` serves a `404` there even with the line set, and the page says so.

Everything under `/api/` other than those two backs the viewer's own bundle and
is private — it changes with the frontend. So is the markup: the interface is
the URL, not the DOM. No page view touches the conversation record; the stats
pages do fold a derived rollup into the index, which is the rebuildable
projection. The server binds loopback only, since it serves the whole archive
with no auth (a non-loopback bind needs `THREAD_ARCHIVE_WEB_NONLOCAL=1`).

**One endpoint writes**: `POST /api/upload` takes an account-export ZIP and puts
it in the drop zone the cohosting watcher already imports from, which is what
`/upload` is a page for — the import itself stays the watcher's, with the settle
and retain/quarantine rules it already owns. Reading is what a `GET` can do; every
other method on every other path is a `405`. A write carries two guards past the
Host check every request passes: a loopback `Origin`, and an `X-Archive-Upload`
header — which no cross-origin form can set, so a page this server did not serve
must first win a preflight that is never answered.

**Runtime is node-free**: the bundle is built ahead of time and committed under
`_web/static/`, so the install never touches node. Node is a *build*-only tool:

```bash
# rebuild the bundle after editing the frontend (node only here):
cd frontend && npm install && npm run build   # → ../src/thread_archive/_web/static/
```

**Archive-links.** With that persistent server, the archive owns the editor
"open this conversation" link itself: `GET /api/archive-link?id=<session-uuid>&source=claude-code`
resolves the session to its thread via `ImportState` and returns `{thread_id, url}`,
or `&redirect=1` → a `302` to `/archive/<id>`. (Local — no separate backend
involved.) `id` may repeat — a caller that cannot tell which uuid it holds is the
session id sends every candidate, best guess first, and the first that resolves
wins; ids that were never imported are skipped, not fatal.
