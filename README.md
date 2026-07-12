# thread-archive

A serverless-native, single-user local archive for AI conversations.

It watches local AI-tool stores, imports provider transcripts into one event
model, writes an append-only **JSONL truth log**, rebuilds a **SQLite** index from
that truth, and searches/reads conversations locally — exposed as a Python library
and over MCP (`thread_search`, `thread_read`).

There is one storage/runtime path: **JSONL + SQLite**. No Postgres, no Neo4j, no
external search engines, no ops service. JSONL is truth; SQLite is a rebuildable
projection — deleting `index.db` and running `archive reindex` loses nothing.

It is a standalone, dependency-free package: a conversation-archive engine with
no server, no external services, and no ties to any host application.

## Install

Installs the `thread_archive` setup command, the `archive` operator CLI,
both MCP servers (`archive-mcp`, `archive-librarian-mcp`), and the pre-built
web viewer — no node. pip needs Python ≥ 3.11; Homebrew brings its own.

```bash
brew install ellamental/thread-archive/thread-archive   # macOS (Homebrew tap)
# or
pip install thread-archive                 # lexical core (+ Leiden community detection)
pip install 'thread-archive[embeddings]'   # optional: local semantic search (heavy: torch)

thread_archive                             # then: run setup
```

The `[embeddings]` extra is pip-only — the brew formula installs the lexical
core.

`thread_archive` is the front door. On first run it discovers this machine's
conversation stores and shows what it found — counts, sizes, date ranges —
*before* touching anything, then asks: import (all, a selection, or skip),
install the always-on watcher (macOS LaunchAgent; includes the web viewer at
:8787), and wire the MCP servers into detected clients (the `claude` CLI, or
it prints the JSON block for any other client). Every choice is skippable and
persists in `<home>/config.json`; a disabled source stays disabled across
every ingest path. Re-running `thread_archive` shows status; `thread_archive
setup` revisits the choices. Non-interactive (agents, scripts):
`thread_archive --yes` accepts every default — without `--yes`, a non-TTY run
only prints guidance and never ingests.

Skipped the watcher? Still covered: `archive-mcp` cohosts **lazy catch-up
ingest** — a background pass at startup and (throttled) around tool calls
imports whatever landed in your local AI-tool stores since the last pass. On
a fresh archive give the first pass a minute to chew before expecting search
hits; the watcher install (`thread_archive setup`, or `archive daemon
install`) is the always-fresh upgrade. With the daemon installed, the MCP
servers' lazy passes degrade to no-op lock probes — exactly one process
ingests at a time, however many Claude Code sessions are open.

**The clone path — for curation.** Clone the repo, open it in Claude Code, and
say *"install this — follow claude-install.md"*. The clone carries what a pip
install doesn't: the `.mcp.json` template and the `/librarian` skill (+ its
enforcement hook) that drives topic-graph curation. Take this route when you
want the knowledge layer worked, not just search.

```bash
git clone https://github.com/ellamental/thread_archive.git thread-archive && cd thread-archive
claude     # then: "install this, following claude-install.md"
```

**Manual path (from a clone).**

```bash
python3 -m venv .venv
.venv/bin/pip install -e .                 # lexical core (+ Leiden community detection)
.venv/bin/pip install -e '.[embeddings]'   # optional: local semantic search (heavy: torch)
.venv/bin/pytest tests/ -q                 # confirm green (add `-m package` for the wheel/sdist release lane)

# wire the two MCP servers into this clone's .mcp.json (absolute venv paths)
sed "s|ABSOLUTE_REPO_PATH|$(pwd)|g" .mcp.json.example > .mcp.json

# bring conversations in, then check
.venv/bin/archive watch --once             # or: archive import <path> --provider <name>
.venv/bin/archive status
```

Restart Claude Code in the repo so it loads `.mcp.json` (the search + librarian MCP
servers) and `.claude/` (the `/librarian` skill + its enforcement hook). Then curate the
topic graph — `/librarian` interactively, or for a large backlog the bulk driver:

```bash
.venv/bin/python scripts/librarian_backfill.py --workers 4 --batch 25
```

## CLI

Two commands, split human from operator. **`thread_archive`** is the human
front door — first-run setup (discover → consent → import → watcher → MCP
wiring), then the status view; `thread_archive setup` re-enters the flow.
**`archive`** is the operator seam:

```bash
archive import <path>     # import a transcript / provider store
archive watch             # watch local AI-tool stores and import incrementally
archive reindex           # rebuild index.db from the JSONL truth directory
archive verify            # integrity check: truth parses + matches the index
archive repair            # quarantine damaged truth lines; restore committed content from the index
archive backup <dest>     # mirror the truth dir (keeps hardlink generations under <dest>/.generations)
archive restore-drill <dest>  # prove the backup restores: rebuild an index from the mirror + smoke read/search
archive nightly <dest>    # the scheduled pipeline: backup → verify (age-gated escalation) → restore drill
archive status            # archive health / counts / last verify + backup + drill outcomes
archive daemon <action>   # macOS: install/uninstall/restart/status the always-on watcher LaunchAgent
```

The CLI is private operational tooling (see Stability below) — the process
seam the LaunchAgents, cron, and operators use. Retrieval deliberately has no
CLI verbs: search and read are the public `archive-mcp` tools, and the web
viewer is cohosted by `archive watch --web`.

## Layout

```text
src/thread_archive/
  _api.py           # internal coordination layer the CLI / MCP / web call into
  cli.py            # the `archive` command
  _setup/           # the `thread_archive` command: first-run setup + status
  _config.py        # truth dir + index path resolution, config.json (source opt-outs)
  _store/           # SQLite store + schema
  _truth/           # JSONL truth log + reindex
  _ops/             # durability kit: backup/mirror + restore drill, verify tiers, nightly, health records
  _importers/       # incremental import orchestration
  _retrieval/       # FTS5 + vector search, read reconstruction
  _knowledge/       # topic graph: event-sourced curation + Leiden analytics
  _watcher/         # local-source watcher (self-feeding ingest)
  _mcp/             # library-native MCP servers (read + librarian)
  _web/             # read-only viewer: stdlib server + built bundle (cohosted by `watch --web`)
  _launchd.py       # `archive daemon`: generates + loads the watcher LaunchAgent (macOS)
  _thread_import/   # vendored provider parsers (a clean, dependency-free island)
frontend/           # the viewer's React+Vite source (dev-only; builds into _web/static/)
host/               # operator layer: Makefile over `archive daemon`, backup agent, family-manifest writer
scripts/            # operator tools (e.g. the librarian backfill driver)
tests/install/      # isolated Docker install test + fixtures
```

## Stability

The public API is exactly two things:

- **the retrieval MCP tools** — `thread_search` and `thread_read`, served by
  `archive-mcp`;
- **the on-disk truth format** — versioned by `manifest.json`'s `version` and
  specified in [docs/format.md](https://github.com/ellamental/thread_archive/blob/main/docs/format.md).
  Data written by one release
  stays readable by the next; a reader refuses a truth directory newer than it
  understands. Programmatic read access to the documented stores (`index.db`
  is plain SQLite; the truth directory is documented JSONL) rides on this
  contract.

Everything else is private support machinery and may change without notice:
the `thread_archive` and `archive` CLIs, the librarian MCP server, the web
viewer, and every Python module — there is **no public Python API**. More surface gets exposed
deliberately as it matures. `tests/test_public_api.py` ratchets the boundary.

## Web viewer

The always-on watcher cohosts a local search + reader UI: `archive watch --web`
(the shipped LaunchAgent passes it) serves at `http://127.0.0.1:8787` — a stdlib
HTTP server handing out a pre-built React bundle plus a few JSON endpoints, in
the watcher's *own* process. One process, one SQLite engine — the viewer reads
concurrently with the watcher's writes, which WAL makes safe (`_store/_base.py`).
No second daemon, and no standalone `web` verb: the viewer exists where the
persistent URL is.

**Runtime is node-free**: the bundle is built ahead of time and committed under
`_web/static/`, so `pip install` never touches node. Node is a *build*-only tool:

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

## What it does

- **Ingests 9 providers** into one event model — Claude Code, Codex, Grok, Antigravity,
  cloth, Cowork (transcript line-streams) and Cursor, OpenCode, Claude Science (SQLite
  scanners). Imports are idempotent, atomic, and survive a full reindex losslessly. A
  `cc-exthost` watcher also recovers mid-turn Claude Code steering messages that never
  reach the session JSONL.
- **Self-feeds** — the watcher tails local stores and ingests incrementally; events land
  in the JSONL truth *before* their commit (no checkpoint in the hot loop). Zero-daemon
  by default (`archive-mcp` cohosts lazy catch-up ingest), with a one-command macOS
  LaunchAgent upgrade (`archive daemon install`) for always-fresh — no external service
  either way.
- **Declares itself** — the installer writes the thread-family manifest
  `<home>/product.json` (`host/write-manifest.py`; `make install-agent` runs
  it), so family consumers discover the archive by enumeration. Spec:
  `docs/spec/product-json.md` in the thread monorepo.
- **Searches locally** — FTS5 lexical (boolean / phrase / pipe-OR / code-identifier),
  optionally fused with local semantic vectors and a cross-encoder re-rank; plus
  transcript reconstruction for reading.
- **Rebuilds losslessly** — `rm index.db && archive reindex` reconstructs the entire
  index from the JSONL truth; a `cp`/`rsync` of the truth dir *is* the backup.
- **Curatable** — an event-sourced topic graph with Leiden communities (see below),
  driven on demand by the `/librarian` skill.

## MCP

Two servers, split read from write. **`thread-archive`** (`archive-mcp`) serves the
read-only tools — `thread_search` / `thread_read` — and cohosts lazy catch-up
ingest in its own process (throttled, cross-process-safe via the ingest-owner
lock; `THREAD_ARCHIVE_MCP_INGEST=0` disables it). **`thread-archive-librarian`**
(`archive-librarian-mcp`) is the curatorial *write* surface — topic/link/citation
writes + the reads a librarian needs (`review_queue`, `topic_search`,
`thread_user_messages`). Keeping them separate means a read-only client never gets
curation power. Client config:

```json
{
  "mcpServers": {
    "thread-archive": {
      "command": "archive-mcp",
      "env": { "THREAD_ARCHIVE_HOME": "~/.thread/archive" }
    },
    "thread-archive-librarian": {
      "command": "archive-librarian-mcp",
      "env": { "THREAD_ARCHIVE_HOME": "~/.thread/archive" }
    }
  }
}
```

## Knowledge layer (the topic graph)

There are exactly two kinds of thread: imported **conversations** and curated **topics**
(`thread_type='topic'`). A topic is modeled as a thread on purpose — so the graph's edges
(`thread_links`) and message→topic citations (`topic_messages`) reference one id space.
Reads/analytics live in the knowledge layer (`_knowledge/`) — PageRank,
communities (**Leiden**, the algorithm Neo4j GDS ran, with a networkx-Louvain fail-soft
fallback), bridges, peers.

Curation is **event-sourced**. Every write (`create_topic`, `link_threads`,
`add_topic_evidence`, `merge_topics`, …) appends a `KgEvent` to an
append-only `truth/kg_events.jsonl` and folds it into the SQLite projection in one
transaction. The log is the source of truth for curation; `thread_links` / `topic_messages`
are rebuildable from it — `reindex` replays the log (idempotent upsert + tombstone) to
reconstruct them, so an unlink/merge/archive is recorded history, never silent loss. The
librarian skill (`.claude/skills/librarian/`) drives the write MCP to clear the
citation backlog on demand (a conversation is 'reviewed' once it gains its first
citation/link — there is no per-thread summary).

## License

MIT — see [LICENSE](https://github.com/ellamental/thread_archive/blob/main/LICENSE).

## Origin

thread-archive is the standalone member of a larger personal project ("thread"), built to
stand on its own — serverless, dependency-free, no backend or external services. The
package is self-contained, but it's young: expect the occasional rough edge or stray
reference to its parent project.
