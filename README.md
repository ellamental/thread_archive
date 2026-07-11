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

**The easy path — clone and let Claude install it.** Clone the repo, open it in Claude
Code, and say *"install this — follow claude-install.md"*. That guide walks an instance
through the whole setup: venv, package install, MCP wiring, importing your conversations,
and curating the topic graph. It's the recommended route.

```bash
git clone <repo-url> thread-archive && cd thread-archive
claude     # then: "install this, following claude-install.md"
```

**Manual path.** Needs Python ≥ 3.11.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .                 # lexical core (+ Leiden community detection)
.venv/bin/pip install -e '.[embeddings]'   # optional: local semantic search (heavy: torch)
.venv/bin/pytest tests/ -q                 # confirm green

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

```bash
archive import <path>     # import a transcript / provider store
archive watch             # watch local AI-tool stores and import incrementally
archive search "<query>"  # search conversations
archive read <thread_id>  # read a conversation
archive web               # on-demand local web UI (search + reader); Ctrl-C to stop
archive reindex           # rebuild index.db from the JSONL truth directory
archive verify            # integrity check: truth parses + matches the index
archive repair            # quarantine damaged truth lines; restore committed content from the index
archive status            # archive health / counts / last verify + backup outcomes
```

## Layout

```text
src/thread_archive/
  api.py            # the public Python library surface (thread_archive.*)
  cli.py            # the `archive` command
  config.py         # truth dir + index path resolution
  store/            # SQLite store + schema
  truth/            # JSONL truth log + reindex
  importers/        # incremental import orchestration
  retrieval/        # FTS5 + vector search, read reconstruction
  knowledge/        # topic graph: event-sourced curation + Leiden analytics
  watcher/          # local-source watcher (self-feeding ingest)
  mcp/              # library-native MCP servers (read + librarian)
  web/              # `archive web`: stdlib server + the built viewer (static/)
src/thread_import/  # vendored provider parsers (a clean, dependency-free island)
frontend/           # the viewer's React+Vite source (dev-only; builds into web/static/)
host/               # `archive watch` LaunchAgent (live ingest)
scripts/            # operator tools (e.g. the librarian backfill driver)
tests/install/      # isolated Docker install test + fixtures
```

## Web viewer

`archive web` serves a local search + reader UI over the same library surface
(`search` / `read_thread_structured` / `status`) — a stdlib HTTP server (no extra
runtime dependency, not a daemon: Ctrl-C stops it) handing out a pre-built React
bundle plus a few JSON endpoints. **Runtime is node-free**: the bundle is built
ahead of time and committed under `web/static/`, so `pip install` never touches
node. Node is a *build*-only tool.

```bash
archive web                       # serve at http://127.0.0.1:8787, open a browser
archive web --port 9000 --no-open

# rebuild the bundle after editing the frontend (node only here):
cd frontend && npm install && npm run build   # → ../src/thread_archive/web/static/
```

**Persistent URL — the watcher cohosts it.** `archive web` is on-demand (Ctrl-C
stops it), so for a stable address the always-on watcher serves the viewer in its
*own* process: `archive watch --web` (the shipped LaunchAgent passes it). One
process, one SQLite engine — the viewer reads concurrently with the watcher's
writes, which WAL makes safe (`store/_base.py`). No second daemon.

**Archive-links.** With that persistent server, the archive owns the editor
"open this conversation" link itself: `GET /api/archive-link?id=<session-uuid>&source=claude-code`
resolves the session to its thread via `ImportState` and returns `{thread_id, url}`,
or `&redirect=1` → a `302` to `/archive/<id>`. (Local — no separate backend
involved.)

## What it does

- **Ingests 9 providers** into one event model — Claude Code, Codex, Grok, Antigravity,
  cloth, Cowork (transcript line-streams) and Cursor, OpenCode, Claude Science (SQLite
  scanners). Imports are idempotent, atomic, and survive a full reindex losslessly. A
  `cc-exthost` watcher also recovers mid-turn Claude Code steering messages that never
  reach the session JSONL.
- **Self-feeds** — the watcher tails local stores and ingests incrementally; events land
  in the JSONL truth *before* their commit (no checkpoint in the hot loop). Ships as a
  macOS LaunchAgent (`host/`), so no external service is needed.
- **Declares itself** — the installer writes the thread-family manifest
  `<home>/product.json` (`python -m thread_archive.manifest`; `make install-agent`
  runs it), so family consumers discover the archive by enumeration. Spec:
  `docs/spec/product-json.md` in the thread monorepo.
- **Searches locally** — FTS5 lexical (boolean / phrase / pipe-OR / code-identifier),
  optionally fused with local semantic vectors and a cross-encoder re-rank; plus
  transcript reconstruction for reading.
- **Rebuilds losslessly** — `rm index.db && archive reindex` reconstructs the entire
  index from the JSONL truth; a `cp`/`rsync` of the truth dir *is* the backup.
- **Curatable** — an event-sourced topic graph with Leiden communities (see below),
  driven on demand by the `/librarian` skill.

## MCP

Two servers, split read from write. **`thread-archive`** (`archive-mcp`) is the
read-only surface — `thread_search` / `thread_read`. **`thread-archive-librarian`**
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
Reads/analytics live in `thread_archive.knowledge` — PageRank,
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

MIT — see [LICENSE](LICENSE).

## Origin

thread-archive is the standalone member of a larger personal project ("thread"), built to
stand on its own — serverless, dependency-free, no backend or external services. The
package is self-contained, but it's young: expect the occasional rough edge or stray
reference to its parent project.
