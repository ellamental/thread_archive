# thread-archive

[![CI](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml/badge.svg)](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://github.com/ellamental/thread_archive)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-black)](https://github.com/ellamental/thread_archive)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Thread Archive is a local-first archive for the AI agents that work on your machine — built by Claude Code, for Claude Code.** Preservation is the product: every session your agent harnesses record lands in one durable, append-only archive you own, on your own disk — kept safe past harness rotation, provider format drift, and index corruption, and served back to your agents over MCP. Claude Code is the supported, first-class source; the other harnesses it reads — Codex, Cursor, OpenCode, Grok, and friends — are best-effort and community-maintainable (see *When an import drifts*). Web chats (claude.ai, ChatGPT, xAI) import too, from account exports you download by hand and drop on the viewer's upload page; the live, self-feeding path is the agent tooling.

**Day one is the demo.** The history already exists — your Claude Code sessions are sitting in `~/.claude` right now, as JSONL nothing can search and the harness eventually rotates away. Point archive at them, and minutes later ask, mid-conversation:

> *"what did we decide about the auth flow in March?"*

Your agent calls `thread_search`, the right conversation comes back, and `thread_read` replays the decision with everything around it. No workflow to adopt, no notes you were supposed to be taking — the record was being written all along.

**Searchable by you — and by your AI.**
- Full-text and semantic search with reranking, filterable by time, source, tool, and content type; an empty query browses recent activity.
- Exposed over MCP (`thread_search`, `thread_read`), so Claude (or any MCP client) can search and read your entire history mid-conversation.
- The same two tools are CLI verbs — `thread_archive search "auth flow" --since 30d`, `thread_archive read <id>` — one implementation behind both, so what you get at a prompt is what your agent gets.
- Search is the access layer over the archive, not the archive itself — an agent typically fires several searches, reformulates, and reads around a hit, and the archive underneath guarantees the conversation is *there* to find. Quality is measured against the archive's own logged usage — real queries, real follow-up reads — with a CI gate that alarms on collapse; the numbers, the protocol, and its limits live in [docs/search-quality.md](docs/search-quality.md), and `thread_archive eval` runs the same self-checkup read-only on your own archive.

**Indexed by code, not just by words.** Every path your agents' tools named — each
`Edit`, `Read`, `Write`, `apply_patch` header, and path-shaped shell argument, in
every provider's spelling — is folded into a structural index. It arrives as two
new scopes on the tools that already exist, because the questions are the ones
search already had shapes for — list the conversations, or search inside them:

- `thread_search(path='rank.py')` — *which conversations worked on this file*. With
  an empty query it is the list, ordered changes-before-looks, each row carrying its
  op tally and opening at the touch rather than at the session's tail; with a query
  it scopes the search to those sessions. A directory asks about a whole repo or
  module (`path='/repo', path_ops='edit,write,delete'`), a glob about a file type.
- `thread_search(commit='31bade5')` — *which conversations this commit is made of*,
  the loop back from `git blame`. Not one session: a commit carries work from several
  sittings, so it resolves to every session whose edits fall inside the commit's
  authorship window — after each of its files was last committed, up to this commit —
  ranked by how much of it they account for. The session that *ran* `git commit` is
  flagged among them rather than standing in for them, which matters most where you
  commit by hand and it is nobody.
- `thread_read(thread_id, summary='files')` — the same index backwards: *what this
  session actually changed.*

The index is a disposable projection of the event log: it backfills itself over an
existing archive and rebuilds with `reindex`.

**Built like a database, not a folder of exports.**
- Plain JSONL files are the source of truth — human-readable, greppable, yours. The search index is disposable and rebuilds from them at any time.
- Crash-safe writes with intent journaling, fsync discipline, and automatic recovery. Your history survives power loss, killed processes, and corrupted indexes.
- Built-in backup, integrity verification, and restore drills: recovers from corruption or an errant delete, and the nightly pipeline checks that the backup actually restores. The archive is ordinary files on disk — whatever backs up the rest of your data covers it the same way.

**Fixes itself where it broke.** A provider's transcript format drifts on the provider's schedule, not a maintainer's. Archive makes that drift loud and locally repairable: drift ledgers and a nightly coverage check catch the degradation, the raw source files are quarantined before the provider prunes them, the in-session search notice names the remedy, and `thread_archive fix-import <provider>` scaffolds an override patch — module, tests, evidence, real samples, and the repair protocol — so the fix gets written on the machine that has the samples, by you or by an agent you hand the scaffold to. The patch goes live only when its scaffolded test suite passes in a fresh subprocess, then re-import recovers everything consumed during the gap. The supported provider's worst case is *preserved but partially modeled until fixed* — and the fix doesn't wait on a release.

**No hosted backend. No cloud. No subscription to lose your history to.** A background watcher keeps it current; every process — the MCP server, the web viewer, the daemons — runs locally, on your machine.

**Dev tooling for one well-provisioned workstation.** macOS and Linux are the supported platforms — the always-on daemons are launchd LaunchAgents on macOS and systemd `--user` units on Linux (both lanes CI-tested; macOS has the most mileage) — and the archive is single-user, single-machine. It assumes workstation-class headroom, too: optional semantic search keeps a multi-GB torch model resident, a normal cost on the machine this is for.

## How it works

Serverless-native, single-user, single-machine: it watches this machine's
agent-harness stores and imports provider transcripts into one event model.

It is a standalone package — no hosted backend, no cloud service, no host
application it depends on. The supported interfaces are the retrieval tools
(over MCP, or as the `search` / `read` CLI verbs), the documented on-disk
format, and the provider plugin API (see Stability) — there is no other public
Python API.

## Install

Python ≥ 3.12, macOS or Linux:

```bash
pip install thread-archive        # or: uv tool install thread-archive
thread_archive setup
```

**`setup` is the onboarding.** On first run it discovers this machine's
conversation stores and shows what it found — counts, sizes, date ranges —
*before* touching anything, then asks: import (all, a selection, or skip),
install the always-on watcher (launchd on macOS, systemd on Linux; includes
the web viewer at :8787), **schedule a nightly backup** (a second question —
*where should backups go?* — that installs the daily backup → verify →
restore-drill pipeline to a disk you name), and wire the MCP server into
detected clients (the `claude` CLI, or it prints the JSON block for any other
client). Every choice is skippable and persists in `<home>/config.json`; a
disabled source stays disabled across every ingest path. The end state is a
populated, searchable archive served over MCP, plus the `thread_archive`
operator CLI and the pre-built web viewer (no node at any point).
`thread_archive status` shows status; `thread_archive setup` revisits the
choices. Non-interactive (agents, scripts): `thread_archive setup --yes`
accepts every default — without `--yes`, a non-TTY run only prints guidance
and never ingests.

Local semantic search is optional and heavy (pulls torch — sized for a dev
machine): `pip install 'thread-archive[embeddings]'`.

**Without the wizard.** The same pieces by hand: `thread_archive watch --once`
runs one ingest pass over this machine's stores (or `thread_archive import
<path> --provider <name>` brings in a single transcript or store),
`thread_archive status` confirms it landed, `thread_archive daemon install`
upgrades to the always-on watcher, and the JSON block under [MCP](#mcp) wires
the server into any client — `archive-mcp` lands on the same PATH as
`thread_archive`. Restart the client so it loads the server.

Skipped the watcher? Still covered: setup-generated MCP entries explicitly set
`THREAD_ARCHIVE_MCP_INGEST=1`, opting `archive-mcp` into **lazy catch-up
ingest**. A background pass at startup and (throttled) around tool calls imports
whatever landed in your local AI-tool stores since the last pass. A bare
`archive-mcp` invocation without that setting is fully read-only. On a fresh
archive give the first pass a minute to chew before expecting search hits; the
watcher install (`thread_archive setup`, or `thread_archive daemon install`) is the
always-fresh upgrade. With the daemon installed, opted-in MCP passes degrade to
no-op lock probes — exactly one process ingests at a time, however many clients
are open.

**From source.** The development install is a clone with an editable venv:

```bash
git clone https://github.com/ellamental/thread_archive.git thread-archive && cd thread-archive
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'          # add -e '.[embeddings]' for local semantic search
.venv/bin/pytest tests/ -q                 # confirm green (add `-m package` for the wheel/sdist release lane)

# wire the read MCP server into this clone's .mcp.json (absolute venv path)
sed "s|ABSOLUTE_REPO_PATH|$(pwd)|g" .mcp.json.example > .mcp.json
```

Or let the agent do it: open the clone in Claude Code and say *"install this,
following [claude-install.md](https://github.com/ellamental/thread_archive/blob/main/claude-install.md)"*
— venv, tests green, MCP wiring, first import, asking you exactly once
(embeddings or not). On Ubuntu, hand it
[claude-install-ubuntu.md](https://github.com/ellamental/thread_archive/blob/main/claude-install-ubuntu.md)
instead — same flow, systemd for the always-on pieces.

A clone's absolute path is baked into its `.mcp.json` wiring and any service
units installed from it, so relocating the clone means re-running that wiring
plus `thread_archive daemon restart`, not a plain `mv`. A clone updates by
fast-forwarding to a release tag (`thread_archive self-update`); a pip install
updates with `pip install -U thread-archive`.

Skipped the watcher? Still covered: setup-generated MCP entries explicitly set
`THREAD_ARCHIVE_MCP_INGEST=1`, opting `archive-mcp` into **lazy catch-up
ingest**. A background pass at startup and (throttled) around tool calls imports
whatever landed in your local AI-tool stores since the last pass. A bare
`archive-mcp` invocation without that setting is fully read-only. On a fresh
archive give the first pass a minute to chew before expecting search hits; the
watcher install (`thread_archive setup`, or `thread_archive daemon install`) is the
always-fresh upgrade. With the daemon installed, opted-in MCP passes degrade to
no-op lock probes — exactly one process ingests at a time, however many clients
are open.

## CLI

One namespaced command. **`thread_archive setup`** runs the wizard (discover →
consent → import → watcher → backup → MCP wiring); **`thread_archive status`**
is the status view. **`search`** and **`read`** are retrieval — the MCP tools at
a terminal. The operator verbs are their siblings:

```bash
thread_archive search [query]    # the thread_search tool: filters by time, source, tool, content type,
                          #   file (--path) or commit; no query browses recent threads;
                          #   --group browse lists matched threads, --output linkable emits JSON
thread_archive read <id>         # the thread_read tool: replay a thread (--mode user|chat|full|last|ends,
                          #   --summary files, --around-event <id> to open a search hit);
                          #   takes a ULID, a legacy integer id, or a provider session uuid
thread_archive import <path>     # import a transcript / provider store
thread_archive providers         # list registered providers (built-in + installed plugins)
thread_archive watch             # watch local AI-tool stores and import incrementally
thread_archive reindex           # rebuild index.db from the JSONL truth directory
thread_archive migrate           # migrate older truth, then reindex and verify
thread_archive embed             # embed user/text events still missing a vector (incremental catch-up;
                          #   --rebuild re-embeds everything)
thread_archive verify            # integrity check: truth parses + matches the index
thread_archive repair            # quarantine damaged truth lines; restore committed content from the index
thread_archive backup <dest>     # mirror the truth dir (hardlink generations under <dest>/.generations) +
                          #   the recovery bundle under <dest>/.recovery (config, retained exports,
                          #   health/ledger snapshots)
thread_archive restore-drill <dest>  # prove the backup restores: rebuild an index from the mirror + smoke read/search
thread_archive restore <mirror> --to <home>  # actually restore: staged rebuild + verify, then atomic publish
                          #   (--generation <stamp> picks a retained snapshot; --list-generations shows them)
thread_archive nightly <dest>    # the scheduled pipeline: backup → verify (age-gated escalation) → restore drill → coverage
thread_archive coverage          # capture-coverage check: source stores reconciled against the archive
thread_archive mirror            # mirror raw harness source stores into <home>/source-mirror
                          #   (verbatim, gzip; nothing ever deleted)
thread_archive eval              # search-quality self-checkup on your own archive (read-only; --from-log
                          #   scores real mined queries, --behavior reports usage rates — see
                          #   docs/search-quality.md)
thread_archive status            # archive health / counts / last verify + backup + drill + coverage outcomes
thread_archive daemon <action>   # install/uninstall/restart/status a service agent (launchd on macOS,
                          #   systemd --user on Linux) — the always-on
                          #   watcher (default), --mcp the shared server, --backup the nightly
                          #   pipeline (`daemon install --backup --dest <path> [--at HH:MM]`), or
thread_archive self-update       # source clones only: fast-forward to the newest release tag — operator-
                          #   driven, nothing updates on its own (--check reports without applying;
                          #   a pip install updates with `pip install -U thread-archive`)
```

`search` and `read` are supported surface — the same implementation
`archive-mcp` serves, so a query typed here and the same query asked
mid-conversation return the same answer. Everything else in the CLI is private
operational tooling (see Stability below): the process seam the service agents,
cron, and operators use. The third door onto the same archive is the web viewer,
cohosted by `thread_archive watch --web`.

## Layout

```text
src/thread_archive/
  _api.py           # internal coordination layer the CLI / MCP / web call into
  cli.py            # the `thread_archive` command — every verb, incl. `setup`
  _setup/           # the wizard behind `thread_archive setup`: first-run setup + status
  _config.py        # truth dir + index path resolution, config.json (source opt-outs)
  _store/           # SQLite store + schema
  _truth/           # JSONL truth log + reindex
  _ops/             # backup kit: backup/mirror + restore drill, verify tiers, nightly, health records
  _importers/       # incremental import orchestration
  _retrieval/       # FTS5 + vector search, read reconstruction, the code axis (code.py)
  _knowledge/       # knowledge-layer data plane: KgEvent fold + SQL topic reads
  _watcher/         # local-source watcher (self-feeding ingest)
  _mcp/             # the library-native read MCP server
  _web/             # the viewer: stdlib server + built bundle (cohosted by `watch --web`)
  _service/         # `thread_archive daemon`: the watcher / MCP / nightly-backup agents behind a
                      #   platform registry — launchd (macOS) and systemd --user (Linux) backends
  _thread_import/   # vendored provider parsers (a clean, dependency-free island)
  _providers/       # the provider registry: built-in descriptors + plugin discovery
  provider/         # PUBLIC: the plugin API a third-party provider is written against
frontend/           # the viewer's React+Vite source (dev-only; builds into _web/static/)
host/               # operator layer: Makefile over `thread_archive daemon`, family-manifest writer
scripts/            # repo tooling (coverage gate, frontend-build check, license notices)
evals/              # the search lab: quality harnesses + experiments/ (see evals/README.md)
tests/install/      # from-nothing install proofs: clean-container Docker + realistic
                      #   discovery-driven first run (~/.claude-style stores, macOS + Linux)
```

## Stability

The public API is exactly four things:

- **the retrieval tools** — `thread_search` and `thread_read`, served to agents
  by `archive-mcp` and to a person by the `thread_archive search` /
  `thread_archive read` verbs. One implementation, two front doors: their
  parameters, defaults, and output are the same contract either way;
- **the on-disk truth format** — versioned by `manifest.json`'s `version` and
  specified in [docs/format.md](https://github.com/ellamental/thread_archive/blob/main/docs/format.md).
  Data written by one release
  stays readable by the next; a reader refuses a truth directory newer than it
  understands. Programmatic read access to the documented stores (`index.db`
  is plain SQLite; the truth directory is documented JSONL) rides on this
  contract.
- **the provider plugin API** — `thread_archive.provider` and its `parse` /
  `testing` submodules, documented in [docs/providers.md](docs/providers.md).
  A provider maintained outside this repo is written against it and cannot
  follow the private tree's churn, so these names keep working.
- **the web viewer's URLs** — the local UI at `http://127.0.0.1:8787`
  ([below](#web-viewer)): its page routes and the two JSON endpoints other
  programs call. Editor buttons, sibling navbars, and health probes link these
  from outside the repo, so they keep working.

Everything else is private support machinery and may change without notice:
the rest of the `thread_archive` CLI, the viewer's bundle and markup, and every
other Python module. More surface gets exposed
deliberately as it matures. `tests/test_public_api.py` ratchets the boundary,
with the viewer's page routes pinned in `frontend/e2e/route-coverage.spec.ts`
against the route table itself.

Releases (changelog compression, version bump, release commit, annotated tag)
follow [docs/releasing.md](https://github.com/ellamental/thread_archive/blob/main/docs/releasing.md).

## When an import drifts

Providers change their on-disk formats without notice, and no maintainer runs a
harness that exercises every variation at every provider release. Archive's
answer is a support tier plus a repair loop, not a promise nobody can keep:

- **Claude Code is first-class.** Its parser carries the full drift ledger
  (validators, residual preservation, the version tripwire), so its worst
  failure mode is *soft*: content is preserved — unmodeled fields ride along
  under `annotations`, skipped files land on the audit ledgers — but partially
  modeled until fixed. Everything else is best-effort: same machinery where it
  reaches, community-maintainable via the plugin API.
- **Drift is loud.** The skip and validation ledgers plus the nightly coverage
  check produce per-source *degradation verdicts* (`thread_archive coverage` prints
  them; the MCP search notice prepends a one-liner naming the remedy the next
  time you search, which is the moment you care).
- **Preservation doesn't wait for the fix.** A degraded source's recently
  active raw files are snapshotted into `dumps/drift/<source>/` — bounded,
  incremental, never auto-deleted — so a fix that comes months later can still
  recover everything the provider has since pruned.
- **The user's own agent writes the fix.** `thread_archive fix-import <provider>`
  scaffolds an override patch under `<home>/plugins/` (module, tests, collected
  samples, drift evidence, per-provider quirk notes, and a `PROTOCOL.md`
  written to be handed to an agent), leaving one job open: the parse logic.
  Archive runs no agent itself — you work the scaffold, or point yours at it
  under whatever scope you choose, remembering that the samples are transcript
  data an agent should treat as untrusted input (see [SECURITY.md](SECURITY.md)).
  Activation is deterministic — the scaffold's tests must pass in a fresh
  subprocess (including a dedup re-import guard) before the override is enabled
  and the ledger-driven re-import recovers the gap.
- **Patches are temporary by default.** The next self-update retires them (a
  core release is the proper fix's vehicle; if drift persists, the notice
  re-fires and the fix re-runs against the new core). `thread_archive fix-import
  <provider> --pin` keeps yours forever. Every lifecycle step is audited in
  `patch-log.jsonl`, and `thread_archive providers` shows `patched` / `patched
  (pinned)` state.

## Not supported

The scope is deliberately narrow. These are design decisions, not gaps waiting
on a release:

- **More than one machine — and merging archives.** An archive belongs to one
  machine. There is no merge tool, no sync, and no federated search across
  archives — a deliberate single-machine scope, not a technical wall. Thread
  ids are globally-unique ULIDs, so two archives never collide there; what a
  merge would still have to reconcile is the locally-minted **event id** space
  wired through the truth layer — the append-only event log, causality links,
  amendment records — by remapping one archive's ids past the other's.
  Mechanical, but unbuilt. *Moving* an archive to another machine is supported
  — carry the directory, or `thread_archive restore <mirror> --to <home>`;
  running two and reconciling them later is not.
- **Anything but macOS and Linux.** The always-on pieces — watcher, scheduled
  backup, shared MCP server — are launchd LaunchAgents on macOS and systemd
  `--user` units on Linux, and public CI exercises both (the Linux lane against
  a real user systemd). macOS has the most mileage. Windows is not supported,
  and no other platform exists here.
- **More than one user.** No accounts, no authentication, no per-user scoping.
  The web viewer binds to `127.0.0.1` and assumes whoever reaches it owns
  everything in the archive.
- **Live capture of web chats.** claude.ai, ChatGPT, and xAI arrive from account
  exports you download by hand — drop the ZIP on the viewer's `/upload` page (or
  into `<home>/dumps/`, or run `thread_archive import-export`). The self-feeding
  path is the local agent harnesses.
- **Driving a conversation.** The archive preserves and retrieves. It never
  writes back to a harness store and never sends a message.

## Web viewer

The always-on watcher cohosts a local search + reader UI: `thread_archive watch --web`
(the shipped watcher service passes it) serves at `http://127.0.0.1:8787` — a stdlib
HTTP server handing out a pre-built React bundle plus a few JSON endpoints, in
the watcher's *own* process. One process, one SQLite engine — the viewer reads
concurrently with the watcher's writes, which WAL makes safe (`_store/_base.py`).
No second daemon: the viewer exists where the persistent URL is.

`thread_archive web` opens that URL in a browser. An opener, not a server.

**The URLs are a supported interface.** Other programs link into the viewer —
editor "open in archive" buttons, sibling consoles' navbars, health probes — so
these paths keep working:

| Path | What it is |
| --- | --- |
| `/` | landing: recent threads, global search |
| `/search` | search results |
| `/threads` | every thread |
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

## MCP

One server, two explicit process modes. **`thread-archive`** (`archive-mcp`)
serves the read-only `thread_search` / `thread_read` tools. The process is also
read-only by default. Setting `THREAD_ARCHIVE_MCP_INGEST=1` opts it into local
lazy catch-up ingest, throttled and cross-process-safe via the ingest-owner
lock. This server exposes no write surface. Client
config with catch-up enabled:

```json
{
  "mcpServers": {
    "thread-archive": {
      "command": "archive-mcp",
      "env": {
        "THREAD_ARCHIVE_HOME": "~/.thread/archive",
        "THREAD_ARCHIVE_MCP_INGEST": "1"
      }
    }
  }
}
```

The shared HTTP server daemon is read-only by default too. Install it with
`thread_archive daemon install --mcp --mcp-ingest` only when it should own catch-up;
leave the flag off when the watcher already owns ingestion.

## Similar and related projects

Preserving and searching AI conversation history is a crowded space, and a lot
of the work in it is good — session-search neighbors (CASS, ctx, deja-vu,
episodic-memory, synty, and more), agent memory layers (mem0, Letta, Zep), and
the prior art outside AI (notmuch). The annotated survey — including where each
neighbor leads and how thread-archive differs — lives in
[docs/related.md](docs/related.md). The short version of the difference: most
tools treat the harness's own files as the record and their index as a cache
over it; archive treats preservation as the product — its own append-only truth
log, backup with restore drills, and unmodeled provider fields preserved
verbatim.

## License

MIT — see [LICENSE](https://github.com/ellamental/thread_archive/blob/main/LICENSE).
The pre-built web viewer bundle contains third-party open-source packages (all
MIT/ISC/BSD); their license texts ship in
`src/thread_archive/_web/THIRD_PARTY_NOTICES.md`
(regenerate with `scripts/gen_third_party_notices.py` when frontend
dependencies change).

## Origin

thread-archive is the standalone member of a larger personal project ("thread"), built to
stand on its own — self-contained, no hosted backend or external services. It's
young, though: expect the occasional rough edge.
