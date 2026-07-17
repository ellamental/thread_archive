# thread-archive

[![CI](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml/badge.svg)](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.14-blue)](https://github.com/ellamental/thread_archive)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Your AI conversations are your most valuable dataset. Stop losing them.

**Thread Archive is a local-first memory system for everything you and your AI assistants have ever said.** It ingests conversations from every tool you use — Claude Code, Claude.ai, ChatGPT, Cursor, Codex, OpenCode, Grok — into one append-only archive you own, on your machine, forever.

**Built like a database, not a folder of exports.**
- Plain JSONL files are the source of truth — human-readable, greppable, yours. The search index is disposable and rebuilds from them at any time.
- Crash-safe writes with intent journaling, fsync discipline, and automatic recovery. Your history survives power loss, killed processes, and corrupted indexes.
- Built-in backup, integrity verification, and restore drills: recovers from corruption or an errant delete, and the nightly pipeline checks that the backup actually restores. The archive is ordinary files on disk — whatever backs up the rest of your data covers it the same way.

**Searchable by you — and by your AI.**
- Full-text and semantic search with reranking, filterable by time, source, tool, and content type.
- Exposed over MCP (`thread_search`, `thread_read`), so Claude (or any MCP client) can search and read your entire history mid-conversation: *"what did we decide about the auth flow in March?"* just works.
- Redaction with encrypted recovery bundles: scrub secrets from the archive without destroying them irrevocably.

**A memory that organizes itself.** A second MCP server — the librarian — lets an AI agent curate the archive: creating topics, pinning key quotes, and linking related threads into a knowledge graph. The setup wizard schedules the curators too — an hourly librarian and a daily gardener run headlessly against those servers, so the organizing happens on its own, not just when you ask. Every curation act is itself event-sourced, so you can always see who connected what, and why.

**No server. No cloud. No subscription to lose your history to.** A background watcher keeps it current; everything runs locally.

## How it works

Serverless-native and single-user: it watches local AI-tool stores and imports
provider transcripts into one event model. There is one storage/runtime path:
**JSONL + SQLite**. No Postgres, no Neo4j, no external search engines, no ops
service — deleting `index.db` and running `archive reindex` loses nothing.

It is a standalone, dependency-free package with no ties to any host
application, usable as a Python library as well as over MCP.

## Install

Installs the `thread_archive` setup command, the `archive` operator CLI,
both MCP servers (`archive-mcp`, `archive-librarian-mcp`), and the pre-built
web viewer — no node. pip needs Python ≥ 3.14.

```bash
pip install git+https://github.com/ellamental/thread_archive.git   # lexical core (+ Leiden community detection)
# optional: local semantic search (heavy: torch)
pip install 'thread-archive[embeddings] @ git+https://github.com/ellamental/thread_archive.git'

thread_archive                             # then: run setup
```

`thread_archive` is the front door. On first run it discovers this machine's
conversation stores and shows what it found — counts, sizes, date ranges —
*before* touching anything, then asks: import (all, a selection, or skip),
install the always-on watcher (macOS LaunchAgent; includes the web viewer at
:8787), **schedule a nightly backup** (a second question — *where should
backups go?* — that installs the daily backup → verify → restore-drill pipeline
to a disk you name), wire the MCP servers into detected clients (the
`claude` CLI, or it prints the JSON block for any other client), and **schedule
self-curation** — an hourly librarian run (topic citations + a stored summary
per new conversation) and a daily gardener run (merge duplicate topics, grow
the hierarchy), each a headless `claude` spawn that gates on work left and
skips cheaply when the queues are empty (`archive curate librarian|gardener`
runs one by hand). Every choice is
skippable and
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
enforcement hook) that drives topic-graph curation and stored summaries. Take
this route when you want the knowledge layer worked, not just search.

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
servers) and `.claude/` (the `/librarian` skill + its enforcement hook). Then curate:
`/librarian` works the queue until it's empty (topic citations + a stored summary per
thread), or pass a per-run cap (`/librarian 25`) and re-run across sessions for a
large backlog.

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
archive backup <dest>     # mirror the truth dir (hardlink generations under <dest>/.generations) +
                          #   the recovery bundle under <dest>/.recovery (config, redaction keyring,
                          #   retained exports, health/ledger snapshots)
archive restore-drill <dest>  # prove the backup restores: rebuild an index from the mirror + smoke read/search
archive restore <mirror> --to <home>  # actually restore: staged rebuild + verify, then atomic publish
                          #   (--generation <stamp> picks a retained snapshot; --list-generations shows them)
archive nightly <dest>    # the scheduled pipeline: backup → verify (age-gated escalation) → restore drill → coverage
archive coverage          # capture-coverage check: source stores reconciled against the archive
archive redact <thread>   # crypto-shred events (--events for a subset): content out of truth, index,
                          #   search, quotes; the original encrypted under a revocable per-redaction key
archive unredact <key_id> # restore a redaction from its encrypted bundle (key still in the keyring)
archive status            # archive health / counts / last verify + backup + drill + coverage outcomes
archive daemon <action>   # macOS: install/uninstall/restart/status a LaunchAgent — the always-on
                          #   watcher (default), --mcp the shared server, or --backup the nightly
                          #   pipeline (`daemon install --backup --dest <path> [--at HH:MM]`)
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
  _ops/             # backup kit: backup/mirror + restore drill, verify tiers, nightly, health records
  _importers/       # incremental import orchestration
  _retrieval/       # FTS5 + vector search, read reconstruction
  _knowledge/       # topic graph: event-sourced curation + Leiden analytics
  _watcher/         # local-source watcher (self-feeding ingest)
  _mcp/             # library-native MCP servers (read + librarian)
  _web/             # read-only viewer: stdlib server + built bundle (cohosted by `watch --web`)
  _launchd.py       # `archive daemon`: generates + loads the watcher / MCP / nightly-backup LaunchAgents (macOS)
  _thread_import/   # vendored provider parsers (a clean, dependency-free island)
frontend/           # the viewer's React+Vite source (dev-only; builds into _web/static/)
host/               # operator layer: Makefile over `archive daemon`, family-manifest writer
scripts/            # operator tools (coverage gate, retrieval eval)
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

Releases (changelog compression, version bump, release commit, annotated tag)
follow [docs/releasing.md](https://github.com/ellamental/thread_archive/blob/main/docs/releasing.md).

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
- **Redacts without deleting history** — `archive redact` crypto-shreds content
  (truth lines, index rows and free pages, search docs, vectors, citation quotes,
  derived titles) into an encrypted bundle on the append-only redaction log, keyed
  by `<home>/keyring.json` (outside the truth dir — the truth mirror and its dated
  generations hold ciphertext only; the keyring rides the backup's head-only
  `.recovery` bundle so a restore keeps active redactions reversible, with
  `{"backup": {"include_keyring": false}}` in config.json as the ciphertext-only
  opt-out). Reversible while the key is held (`archive unredact`); escrow the key
  off the machine (`--show-key` + `--forget`) or destroy it for crypto-erasure. The
  original provider store keeps its own copy — redaction covers the archive.
- **Guards its operator's reality** — when search once answered "not found"
  against a conversation that was right there, that failure's *mechanism* is
  pinned as a permanent regression test on a synthetic corpus: a false "not
  found" against a high-confidence memory is the one failure class the suite
  guards hardest.
- **Curatable** — an event-sourced topic graph with Leiden communities (see below),
  driven on demand by the `/librarian` skill, which also stores each conversation's
  search-first summary: a few dense sentences indexed into the default search scope
  and embedded for the semantic arm, plus a structured `indexed_summary`
  (event-anchored markdown) for long threads, served by
  `thread_read summary='short'|'indexed'`.

## MCP

Two servers, split read from write. **`thread-archive`** (`archive-mcp`) serves the
read-only tools — `thread_search` / `thread_read` — and cohosts lazy catch-up
ingest in its own process (throttled, cross-process-safe via the ingest-owner
lock; `THREAD_ARCHIVE_MCP_INGEST=0` disables it). **`thread-archive-librarian`**
(`archive-librarian-mcp`) is the curatorial *write* surface — topic/link/citation
writes and stored-summary writes (`thread_set_summary`) + the reads the librarian
needs (`review_queue`, `topic_search`, `thread_user_messages`). Keeping them
separate means a read-only client never gets curation power. Client config:

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

The graph is consumable from the public read surface, not just the librarian's: search
headers name the subjects a result set clusters under with their `[topic <id>]`s,
`thread_read` on a topic id renders the topic's curated page (description, links, cited
quotes — each quote anchored to open via `around_event`), and `thread_search(topic_id=…)`
scopes a search to the topic's member conversations.

Curation is **event-sourced**. Every graph write (`create_topic`, `link_threads`,
`add_topic_evidence`, `merge_topics`, …) appends a `KgEvent` to an
append-only `truth/kg_events.jsonl` and folds it into the SQLite projection in one
transaction. The log is the source of truth for curation; `thread_links` / `topic_messages`
are rebuildable from it — `reindex` replays the log (idempotent upsert + tombstone) to
reconstruct them, so an unlink/merge/archive is recorded history, never silent loss.

The librarian skill (`.claude/skills/librarian/`) drives the write MCP over
`review_queue` — event-bearing conversations still missing either half of its
per-thread output, held back while a thread is still ingesting. Per thread it writes
**~3+ topic citations** and a **stored summary** (`thread_set_summary`): a short,
dense `summary` that immediately becomes a thread-meta search doc (the default search
scope is user + title + summary, and the embed cohost picks it up for the semantic
arm), plus an event-anchored `indexed_summary` for long threads. A conversation is
done once it has both a citation/link and a summary. Summaries are deliberately *not*
kg events: they're thread metadata like the title, made durable by the thread's own
latest-wins truth record, which keeps redaction's thread-meta scrub the single place
summary content ever needs erasing.

## License

MIT — see [LICENSE](https://github.com/ellamental/thread_archive/blob/main/LICENSE).

## Origin

thread-archive is the standalone member of a larger personal project ("thread"), built to
stand on its own — serverless, dependency-free, no backend or external services. The
package is self-contained, but it's young: expect the occasional rough edge or stray
reference to its parent project.
