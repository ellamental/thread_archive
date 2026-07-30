# Dev panels

Three pages whose subject is how the archive is *doing* rather than what it
holds. They are a separate app on a separate server, in a directory that ships in
nothing.

```bash
cd devweb/frontend && npm install && npm run build   # once, and after frontend edits
python -m devweb                                    # → http://127.0.0.1:8789
```

| Path | What it is |
| --- | --- |
| `/retrieval` | how search is performing: served latency per front door and by warm/cold regime, per-stage costs |
| `/telemetry` | web endpoint latency, ingest cost, folded ingest faults, retained ledger size |
| `/lab` | what the bench has to measure with: benchmark rows, corpora on disk, every recorded run |
| `/lab/run/<id>` | one run's per-query detail, optionally diffed against another |

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

The archive's viewer ([web-viewer.md](web-viewer.md)) is for reading
conversations. These are instruments for working on the archive itself, and they
used to be routes inside that same app, mounted by a `dev_panels` line in
`config.json`. That meant the viewer's bundle carried them whether or not anyone
had asked, and the watcher that serves the archive also served them.

Now the split is structural rather than a flag: different bundle, different
process, different port, different directory. There is nothing to switch on,
because there is nothing here to switch. The archive's server 404s their
endpoints and its app has no routes for their addresses.

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
  frontend/      # the React app: four views, its own Vite build
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
- `cd devweb/frontend && npm test` covers the four views (`devweb-test` in
  `ci.toml`, with `devweb-typecheck` beside it).
- `npm run e2e` drives them in Chromium against a production build, API mocked at
  the network boundary — including the drill from the lab's run ledger into one
  run and back. Preview port 4175, so it runs beside the viewer's lane
  (`devweb-e2e`).

There is no committed-bundle byte-comparison row: that one exists for the viewer
because an install has to serve the bundle it committed, and nothing installs
this.
