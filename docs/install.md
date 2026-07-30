# Install

Python ≥ 3.12 (Ubuntu 24.04's default `python3` clears that floor as-is; an
older release needs a newer interpreter first — deadsnakes PPA or pyenv), macOS
or Linux:

```bash
pip install thread-archive        # or: uv tool install thread-archive
thread-archive setup
```

**`setup` is the onboarding.** On first run it discovers this machine's
conversation stores and shows what it found — counts, sizes, date ranges —
*before* touching anything, then asks: import (all, a selection, or skip),
install the always-on watcher (launchd on macOS, systemd on Linux),
**schedule a nightly backup** (a second question —
*where should backups go?* — that installs the daily backup → verify →
restore-drill pipeline to a disk you name), wire the MCP server into detected
clients (the `claude` CLI, or it prints the JSON block for any other client),
and — from a clone, where the viewer exists — open the archive in your browser.
Every choice is skippable and persists in `<home>/config.json`; a disabled
source stays disabled across every ingest path — and across later runs of
`setup`, which only changes a source's policy where you state a new one (the
edit selection). The end state is a populated, searchable archive served over
MCP, plus the `thread-archive` operator CLI. `thread-archive status` shows
status; `thread-archive setup`
revisits the choices. Non-interactive (agents,
scripts): `thread-archive setup --yes` accepts every default — without `--yes`,
a non-TTY run only prints guidance and never ingests, and neither shape opens a
browser.

## Semantic search (optional)

Local semantic search is optional and heavy (pulls torch — sized for a dev
machine): `pip install 'thread-archive[embeddings]'`. It brings the corpus-graph
ranking stack with it (the `[leiden]` extra: `leidenalg` + `igraph`),
since that signal is computed over the vectors. `[all]` is every runtime feature
under one name; the base install is lexical-only and pulls no C extension beyond
what `numpy` and `mcp` already need. `igraph` and `leidenalg` publish narrower
wheel matrices than those, so where pip finds no wheel it builds from source and
wants a C toolchain: the Xcode Command Line Tools (`xcode-select --install`) on
macOS, `build-essential` on Debian/Ubuntu.

## Updating

`pip install -U thread-archive` (or `uv tool upgrade
thread-archive`), then `thread-archive service restart` so the always-on watcher
runs the new code; MCP clients pick it up on their next session. Nothing updates
itself — no probe, no background apply. A source clone updates differently
(below).

## Without the wizard

The same pieces by hand: `thread-archive watch --once`
runs one ingest pass over this machine's stores (or `thread-archive source import
<path> --provider <name>` brings in a single transcript or store),
`thread-archive status` confirms it landed, `thread-archive service install`
upgrades to the always-on watcher, and the JSON block in [mcp.md](mcp.md) wires
the server into any client — `archive-mcp` lands on the same PATH as
`thread-archive`. Restart the client so it loads the server.

Skipped the watcher? Still covered: setup-generated MCP entries explicitly set
`THREAD_ARCHIVE_MCP_INGEST=1`, opting `archive-mcp` into **lazy catch-up
ingest**. A background pass at startup and (throttled) around tool calls imports
whatever landed in your local AI-tool stores since the last pass. A bare
`archive-mcp` invocation without that setting is fully read-only. On a fresh
archive give the first pass a minute to chew before expecting search hits; the
watcher install (`thread-archive setup`, or `thread-archive service install`) is the
always-fresh upgrade. With the daemon installed, opted-in MCP passes degrade to
no-op lock probes — exactly one process ingests at a time, however many clients
are open.

## From source

The development install is a clone with an editable venv:

```bash
git clone https://github.com/ellamental/thread_archive.git thread-archive && cd thread-archive
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'          # add -e '.[embeddings]' for local semantic search
.venv/bin/pytest tests/ -q                 # confirm green (add `-m package` for the wheel/sdist release lane)

# wire the read MCP server into this clone's .mcp.json (absolute venv path)
sed "s|ABSOLUTE_REPO_PATH|$(pwd)|g" .mcp.json.example > .mcp.json
```

Then populate it the same way a packaged install does — `.venv/bin/thread-archive
setup` for the wizard, or `.venv/bin/thread-archive watch --once` for a single
ingest pass — and restart the client so it loads the server.

A clone's absolute path is baked into its `.mcp.json` wiring and any service
units installed from it, so relocating the clone means re-running that wiring
plus `thread-archive service restart`, not a plain `mv`. A clone updates through
git — fetch, check out the newer release tag, `pip install -e .`, restart the
agents. `thread-archive self-update` is for packaged installs and says so when
run from a clone; its code is the checkout, and moving that is git's job.

## Uninstall

`thread-archive uninstall` takes back everything setup put on the
machine — the service agents, the MCP wiring in your client, the family manifest
and monitor heartbeat, and setup's own record in `config.json` — and **never
touches the archive**. Conversations, index, source choices, logs and exports
stay where they are, and `search` / `read` keep answering from them with nothing
installed. It closes by naming **every place the data still is** — the home, a
truth dir or index pointed outside it, every backup mirror any stage ever
recorded (plus one scheduled but not yet run), a home an earlier `restore
--replace` set aside, the `~/.thread_archive` compat symlink — and then how to
finish: `pip uninstall thread-archive`, or the clone to delete when the install
runs from one. Deleting any of the data is yours to do. `--dry-run` reports what
would go without changing anything, `--yes` skips the confirmation. An agent or
client entry serving a *different* archive home is reported and left alone.
