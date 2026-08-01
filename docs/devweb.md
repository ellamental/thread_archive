# Dev panels

Three pages whose subject is how the archive is *doing* rather than what it
holds, and an overview that says which one to open. They are a separate app on a
separate server, in a directory that ships in nothing.

```bash
cd devweb/frontend && npm install && npm run build   # once, and after frontend edits
python -m devweb                                    # → http://127.0.0.1:8789
```

| Path | What it is |
| --- | --- |
| `/` | the overview: each instrument's headline in a panel, one window over all of them |
| `/retrieval` | how search is performing: served latency per front door and by warm/cold regime, per-stage costs, restarts and background rebuilds |
| `/telemetry` | web endpoint latency, ingest cost (including the loop's idle floor), folded ingest faults, retained ledger size |
| `/lab` | what the bench has to measure with: benchmark rows, corpora on disk, every recorded run |
| `/lab/run/<id>` | one run's per-query detail, optionally diffed against another |

**Most of the ledgers under these pages are written only on an install being
developed on** — `"dev_mode": true` in the archive's `config.json`. Served
requests, retrieval calls and ingest passes are a maintainer's instruments, and
an install that is merely run should not accumulate a row per page its operator
opened. Without that line the panels still render, over whatever history is on
disk, and say so at the top of each page and per ledger in `/telemetry`'s
inventory. Ingest faults and load runs are outside the switch and record
everywhere. A single ledger can be moved either way for one process with its own
environment variable — `THREAD_ARCHIVE_USAGE_LOG`, `THREAD_ARCHIVE_WEB_METRICS`,
`THREAD_ARCHIVE_INGEST_LOG` — which outranks the config in both directions.

The overview summarises and does not restate: every panel links to the page that
explains it, and the reasoning lives there. What it may not do is pool. A median
across front doors is a mixture nobody waited on — which is the retrieval page's
whole argument, and a dashboard is exactly where that number would get quoted as
the headline — so the door table comes across per door and the pooled figures
stay off it. Each panel fetches and fails on its own, so one unreadable ledger
costs one panel rather than the page.

**One default window, 14 days** — `DEFAULT_HOURS` in `frontend/src/api.ts`, which
the overview, `/retrieval` and `/telemetry` all open on, and which the client's
own two windowed calls take when nothing is passed. Each page's control still
moves its own window from there. One default across all of them is what keeps the
same door table reading the same on two pages: windows that differ put different
numbers under one heading, and the instruments look like they disagree when it is
one stretch of time against another. On the overview the one control drives retrieval and telemetry
together, since two panels read side by side have to cover the same stretch; the
bench inventory and the run ledger are windowless and do not move with it.

`python -m devweb --open` opens the browser once the server is up; `--port` and
`--host` move it — though the viewer's link to here (below) is fixed at the
default port, since the archive's server has no way to ask where you started
this one.

The archive's viewer can carry a `dev panels ↗` link over, when its operator asks
for one: `thread-archive web dev` sets `"dev_panels": true` in the archive's
`config.json` and the rail grows the link; `web --no-dev` removes it. That switch
moves a link and nothing more — it does not start this server, and the panels are
reachable at the URL above whether or not the viewer names them. A non-loopback bind is refused unless
`THREAD_ARCHIVE_WEB_NONLOCAL=1` — the panels have no auth and read this machine's
ledgers.

## Why it is separate

The archive's viewer ([public/web-viewer.md](public/web-viewer.md)) is for reading
conversations. These are instruments for working on the archive itself, and the
split between the two is structural rather than a flag: different bundle,
different process, different port, different directory. There is nothing to
switch on, because there is nothing here to switch — the archive's server 404s
these endpoints and its app has no routes for their addresses.

Mounting them inside the viewer instead would put them in a bundle every reader
downloads whether or not they ever open one, and make the watcher that serves the
archive serve them too.

**It runs in the foreground.** No service unit, no always-on story — the natural
lifetime is the terminal you started it in. Nothing depends on it: the watcher,
the MCP server and the viewer all run whether or not this is up.

## Shape

```
devweb/
  server.py      # the router (pure) + a GET-only stdlib HTTP server
  telemetry.py   # the /telemetry data layer, over the archive's ledgers
  __main__.py    # python -m devweb
  static/        # the built bundle — NOT committed (see below)
  frontend/      # the React app: the overview, three panels, one run, its own Vite build
```

`server.py` imports the archive's request guards (`_host_allowed`, the security
headers) from `thread_archive._web.server` rather than restating them: a second
copy is a second thing to get wrong, and both servers have the same exposure. It
reads `search_lab/` directly — no fail-soft bridge, because devweb and the lab
ship together, which is to say neither ships.

Nothing in `src/thread_archive` imports `devweb`. The dependency runs one way:
this is a repo-local consumer of private modules, the same freedom `search_lab/`
has, and for the same reason.

**The bundle is not committed**, unlike the viewer's. The viewer's is committed
because an install has to serve a UI without node; nothing installs this, so
there is no such case — and a committed bundle in a dev directory is diff noise
on every frontend edit. `devweb/static/` is gitignored, and the server names the
build command if it is missing rather than 404ing per page.

## Tests

- `tests/test_devweb.py` drives the router socket-free, the way
  `tests/test_web.py` drives the viewer's. It carries the `viewer` marker, so the
  Docker install lane — which runs this suite against the installed wheel —
  stands it down along with every other test of a surface that does not ship.
- `cd devweb/frontend && npm test` covers every view (`devweb-test` in
  `ci.toml`, with `devweb-typecheck` beside it).
- `npm run e2e` drives them in Chromium against a production build, API mocked at
  the network boundary — including the drill from the lab's run ledger into one
  run and back. Preview port 4175, so it runs beside the viewer's lane
  (`devweb-e2e`).

There is no committed-bundle byte-comparison row: that one exists for the viewer
because an install has to serve the bundle it committed, and nothing installs
this.
