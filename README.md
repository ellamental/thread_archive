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
archive reindex           # rebuild index.db from the JSONL truth directory
archive status            # archive health / counts
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
src/thread_import/  # vendored provider parsers (a clean, dependency-free island)
host/               # `archive watch` LaunchAgent (live ingest)
scripts/            # operator tools (e.g. the librarian backfill driver)
tests/install/      # isolated Docker install test + fixtures
```

## What it does

- **Ingests 6 providers** into one event model — Claude Code, Codex, Grok, Antigravity
  (transcript line-streams) and Cursor, OpenCode (SQLite scanners). Imports are
  idempotent, atomic, and survive a full reindex losslessly. A `cc-exthost` watcher also
  recovers mid-turn Claude Code steering messages that never reach the session JSONL.
- **Self-feeds** — the watcher tails local stores and ingests incrementally; events land
  in the JSONL truth *before* their commit (no checkpoint in the hot loop). Ships as a
  macOS LaunchAgent (`host/`), so no external service is needed.
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
      "env": { "THREAD_ARCHIVE_HOME": "~/.thread_archive" }
    },
    "thread-archive-librarian": {
      "command": "archive-librarian-mcp",
      "env": { "THREAD_ARCHIVE_HOME": "~/.thread_archive" }
    }
  }
}
```

## Knowledge layer (the topic graph)

Topics are threads (`thread_type='topic'`); `thread_links` are the edges, `topic_messages`
the message→topic citations. Reads/analytics live in `thread_archive.knowledge` — PageRank,
communities (**Leiden**, the algorithm Neo4j GDS ran, with a networkx-Louvain fail-soft
fallback), bridges, peers.

Curation is **event-sourced**. Every write (`create_topic`, `link_threads`,
`add_topic_evidence`, `merge_topics`, `set_thread_summary`, …) appends a `KgEvent` to an
append-only `truth/kg_events.jsonl` and folds it into the SQLite projection in one
transaction. The log is the source of truth for curation; `thread_links` / `topic_messages`
are rebuildable from it — `reindex` replays the log (idempotent upsert + tombstone) to
reconstruct them, so an unlink/merge/archive is recorded history, never silent loss. The
librarian skill (`.claude/skills/librarian/`) drives the write MCP to clear the
summary/citation backlog on demand.

## License

MIT — see [LICENSE](LICENSE).
