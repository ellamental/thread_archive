# thread-archive

[![CI](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml/badge.svg)](https://github.com/ellamental/thread_archive/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.14-blue)](https://github.com/ellamental/thread_archive)
[![Platform](https://img.shields.io/badge/platform-macOS-black)](https://github.com/ellamental/thread_archive)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Thread Archive is a local-first memory system for the AI agents that work on your machine — built by Claude Code, for Claude Code.** It ingests every session your agent harnesses record into one durable archive you own, on your Mac. Claude Code is the supported, first-class source; the other harnesses it reads — Codex, Cursor, OpenCode, Grok, and friends — are best-effort and community-maintainable (see *When an import drifts*). Web chats (claude.ai, ChatGPT, xAI) import too, from account exports you download by hand; the live, self-feeding path is the agent tooling.

**Day one is the demo.** The history already exists — your Claude Code sessions are sitting in `~/.claude` right now, as JSONL nothing can search and the harness eventually rotates away. Point archive at them, and minutes later ask, mid-conversation:

> *"what did we decide about the auth flow in March?"*

Your agent calls `thread_search`, the right conversation comes back, and `thread_read` replays the decision with everything around it. No workflow to adopt, no notes you were supposed to be taking — the memory was being written all along. Archive keeps it, finds it, and measures the finding against its own logged usage rather than a synthetic benchmark (numbers below).

**Searchable by you — and by your AI.**
- Full-text and semantic search with reranking, filterable by time, source, tool, and content type.
- Exposed over MCP (`thread_search`, `thread_read`), so Claude (or any MCP client) can search and read your entire history mid-conversation.
- Redaction with encrypted recovery bundles: scrub secrets from the archive without destroying them irrevocably.

**Measured against real usage, not a synthetic benchmark.** Every
`thread_search` an agent runs is itself archived, along with the `thread_read`
that followed — so the archive holds a click-labeled query log of its own use.
The eval harness (`scripts/retrieval_eval.py --from-log`) mines those
search→read pairs: each query is one an agent actually ran, and the thread the
agent opened next is the answer that must rank. On a 17k-thread / 3.7M-event
archive, 561 mined cases:

| search stack | MRR | recall@10 | p50 latency |
|---|---|---|---|
| core install (FTS5 lexical) | 0.19 | 0.33 | 0.6 s |
| + local semantic fusion | 0.25 | 0.43 | 0.6 s |
| + cross-encoder rerank | 0.25 | 0.44 | 3.1 s |

Rows are cumulative, and the labels carry a click's limits: the opened thread
was the agent's pick from what search surfaced that day — not a verdict that
nothing better existed — so relevant siblings score as misses, and a stack
that surfaces what past search never could gets no credit for it. Good
numbers here mean the stack reliably re-finds what real searches actually
delivered; they cannot certify there was nothing better to find. Semantic fusion is the layer that pays — +10 points of recall@10
over the lexical core at no latency cost. The cross-encoder adds about two
more for 5× the latency, which is why the pipeline auto-gates it to
conceptual queries instead of running it everywhere. Both model arms have an
off switch — `THREAD_ARCHIVE_EMBED=off` and `THREAD_ARCHIVE_RERANK=off` pin a
process to the lexical core without uninstalling the extra, for a box that
wants search cheap and free of the cold-start model load (`retrieval_eval.py
--lexical-only` measures that configuration). One more signal made the cut:
a **community-coherence re-rank** from the corpus-native embedding graph
(thread centroids → cosine kNN → Leiden — zero curation input, every embedded
conversation a node). Within a ranked pool, threads whose community carries
more of the pool's top mass get a small boost; on this protocol it lifts
recall at every depth past 1 (R@5 0.327→0.341, R@10 0.414→0.433, R@20
0.492→0.508) with MRR flat, and `scripts/graph_eval.py` re-measures it.
It orders the head only when the cross-encoder stands down: the two are
alternative head orderers, and stacking coherence under the rerank measures
as a loss end-to-end (it reshuffles which candidates reach the rerank
window). On by default; `THREAD_ARCHIVE_COHERENCE=off` disables, a float
retunes gamma. Two graph signals were measured and rejected on the same protocol —
kept out of the default stack, opt-in for experimentation: PageRank authority
from the *curated* topic graph (`THREAD_ARCHIVE_GRAPH_RANK=<weight>`)
degrades ranking monotonically with weight, because query-independent
authority floats hub threads over the specific thread a query names. Curated thread summaries move
these numbers by less than a point — whatever their value for browsing and
curation, ranked search does not measurably ride on them. (An earlier
title-as-query eval said otherwise on every count; its queries were LLM
distillations of the threads they named, and it flattered every layer that
searched other distillations. It survives in the harness as a quick local
probe; CI runs a single lean gate — semantic arm verified alive directly,
plus a small seeded sample of the log-mined cases as a collapse alarm.)

The same trail powers three more instruments, each aimed at a limit of the
click labels. Every CI gate run appends its numbers to a trend ledger
(`~/.thread/archive/retrieval-trend.jsonl`), so quality is a time series, not
a launch-day screenshot, and `--mined-after` holds out only the cases mined
after a ranking change shipped. `--behavior` reports zero-label usage
signals — for every search the trail shows whether the agent opened a
result, searched again, or walked away — rates that move only when something
real moves. And `scripts/retrieval_judge.py` runs a sample of the mined
queries through the production stack and has a headless `claude` grade every
top-10 thread, yielding graded precision, a calibration of the click labels
themselves, and explicit credit for relevant results the click protocol can
only score as misses. The judge grades only what production returned, from
snippets; `scripts/retrieval_mine_gold.py` goes the rest of the way — one
headless `claude` *agent* per sampled query reads the originating session
for intent, sweeps the corpus with its own reformulated searches (bounded to
the corpus as of the original search's date), reads candidates, and writes a
corpus-grounded gold case. The output is an eval `--cases` file whose
per-case date bound the scoring search honors, so the one-time mining spend
buys recall-capable, deterministic labels every later eval run scores
against for free. The knowledge graph gets its own usage meter in
[thread-librarian](https://github.com/ellamental/thread_archive_librarian) (`scripts/topic_eval.py` there): **subject
uptake** — how often a topic read follows a search. A lens nobody pivots
through is a terrarium, however well curated; uptake is the number that says
which it is.

The instruments stack into a **quality ladder**, fastest tier first — change
a ranking weight and climb until the evidence matches the stakes:

| tier | what runs | corpus | cost | when |
|---|---|---|---|---|
| 0 | `tests/test_search_quality.py` (in every pytest run) | checked-in synthetic corpus (`tests/quality_corpus.py`), lexical stack | seconds | every change |
| 1 | `pytest -m quality_models` | same corpus, real embedding + rerank models | minutes | touching the model arms |
| 2 | CI `retrieval-gate` row (`retrieval_eval.py --from-log`) | live archive, mined click labels | ~minutes | every commit, via thread-ci |
| 3 | `retrieval_eval.py` by hand, `graph_eval.py`, `retrieval_judge.py`, `search_arena.py`, `--behavior` | live archive | minutes–hours | evaluating a deliberate ranking change |
| 3½ | `retrieval_eval.py --cases` on agent-mined golds (`retrieval_mine_gold.py` to mint them) | live archive, corpus-grounded labels | seconds to score; agent-minutes per mined case | scoring against grounded labels; mining is an occasional cadence |
| 4 | `pytest -m beir` | external BEIR benchmark | tens of minutes | calibrating against published baselines |

Tier 0 is the laboratory bench: known relevance structure, deterministic,
and `run_cases(search=...)` scores any candidate ranker against the incumbent
on identical cases — the A/B seam the higher tiers then validate on real
usage.

That seam has a front door: **the search lab**. Every tunable of the pipeline
(ranking weights, decay constants, pool sizes) lives in one object,
`thread_archive._retrieval.SearchParams`, accepted by `search(params=...)` —
the shipped defaults ARE the production configuration. Each module in
`experiments/` is one candidate configuration (a `SearchParams` value, or a
full `SEARCH` callable for changes params can't express — the contract is in
`experiments/README.md`), and `scripts/search_lab.py` scores the baseline plus
every experiment on identical corpus cases and prints a leaderboard with
deltas: seconds for the lexical stack, `--models` for the fused pipeline. The
corpus carries adversarial structure (a TF-spam paste bm25 loves, a recency
pair whose old twin is the lexically stronger match) precisely so
configurations *separate* — stripping the weighted ranker measurably loses.
A winner here is a direction, not a verdict; promote it by re-measuring on
tiers 2–3 before changing the defaults in `_retrieval/params.py`.

The promotion step has its own instrument: **the arena**
(`scripts/search_arena.py`). It duels a challenger from `experiments/` against
the shipped configuration on real mined queries — both rankings for each
query go to a headless `claude` judge, side order randomized, labels blind —
and reports challenger wins/losses/ties with an exact sign test. Identical
rankings tie without spending a judge call, so cost scales with how much the
configurations actually disagree. Where the lab says "this direction looks
good on the synthetic corpus," the arena says "on real usage, a judge prefers
it" — the bar to clear before touching the defaults.

**Built like a database, not a folder of exports.**
- Plain JSONL files are the source of truth — human-readable, greppable, yours. The search index is disposable and rebuilds from them at any time.
- Crash-safe writes with intent journaling, fsync discipline, and automatic recovery. Your history survives power loss, killed processes, and corrupted indexes.
- Built-in backup, integrity verification, and restore drills: recovers from corruption or an errant delete, and the nightly pipeline checks that the backup actually restores. The archive is ordinary files on disk — whatever backs up the rest of your data covers it the same way.

**Fixes itself where it broke.** A provider's transcript format drifts on the provider's schedule, not a maintainer's. Archive makes that drift loud and locally repairable: drift ledgers and a nightly coverage check catch the degradation, the raw source files are quarantined before the provider prunes them, the in-session search notice names the remedy, and `archive fix-import <provider>` scaffolds an override patch — module, tests, evidence, real samples, and the repair protocol — so the fix gets written on the machine that has the samples, by you or by an agent you hand the scaffold to. The patch goes live only when its scaffolded test suite passes in a fresh subprocess, then re-import recovers everything consumed during the gap. The supported provider's worst case is *preserved but partially modeled until fixed* — and the fix doesn't wait on a release.

**A memory an agent can organize.** The archive carries the *data plane* of an event-sourced topic graph a curating agent can build over it — creating topics, pinning key quotes, linking related threads, tending the hierarchy. Every curation act lands in the archive's truth log, so you can always see who connected what, and why. The curation agents, the graph analytics (PageRank, Leiden communities, bridges), and the topic surfaces live in the separate [thread-librarian](https://github.com/ellamental/thread_archive_librarian) package; without it the graph simply stays empty, and nothing else depends on it.

**No hosted backend. No cloud. No subscription to lose your history to.** A background watcher keeps it current; every process — the MCP server, the web viewer, the daemons — runs locally, on your machine.

**Dev tooling for one well-provisioned Mac.** macOS is the supported platform — the daemons are LaunchAgents, the file locks are Unix — and the archive is single-user, single-machine. It assumes workstation-class headroom, too: optional semantic search keeps a multi-GB torch model resident, a normal cost on the machine this is for.

## How it works

Serverless-native, single-user, single-machine: it watches this Mac's
agent-harness stores and imports provider transcripts into one event model.

It is a standalone package — no hosted backend, no cloud service, no host
application it depends on. The supported interfaces are the MCP tools, the
documented on-disk format, and the provider plugin API (see Stability) — there
is no other public Python API.

## Install

**The clone is the install.** thread-archive is not distributed as a package —
there is no registry; a release is an annotated tag on the repo you can pin
(see [docs/releasing.md](https://github.com/ellamental/thread_archive/blob/main/docs/releasing.md)).
The product is the repo itself: the Python package, the read MCP server, the
`.mcp.json` template that wires it in, and the pre-built web viewer; the venv
lives in the clone once it's built. Clone it, open it in Claude Code, and let
the agent install its own memory:

```bash
git clone https://github.com/ellamental/thread_archive.git thread-archive && cd thread-archive
claude     # then: "install this, following claude-install.md"
```

[claude-install.md](https://github.com/ellamental/thread_archive/blob/main/claude-install.md)
walks the agent through the whole thing — venv, tests green, MCP wiring,
first import — stopping to ask you exactly once: whether to add local
semantic search (heavy: pulls torch). macOS only; Python ≥ 3.14. The end
state is a populated, searchable archive served over MCP, plus the `archive`
operator CLI and the pre-built web viewer (no node).

**Manual path (no agent).** The same install by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .                 # lexical core + the corpus-graph ranking stack (Leiden)
.venv/bin/pip install -e '.[embeddings]'   # optional: local semantic search (pulls torch — sized for a dev machine)
.venv/bin/pip install -e '.[dev]'          # test/lint toolchain — pytest lives here, not in the base install
.venv/bin/pytest tests/ -q                 # confirm green (add `-m package` for the wheel/sdist release lane)

# wire the read MCP server into this clone's .mcp.json (absolute venv path)
sed "s|ABSOLUTE_REPO_PATH|$(pwd)|g" .mcp.json.example > .mcp.json

# bring conversations in, then check
.venv/bin/archive watch --once             # or: archive import <path> --provider <name>
.venv/bin/archive status
```

Restart Claude Code in the repo so it loads `.mcp.json` (the search/read MCP server).

**Always-on — the setup wizard.** Either path leaves a working archive that
ingests lazily. `.venv/bin/thread_archive` upgrades it to always-on: on first
run it discovers this machine's conversation stores and shows what it found —
counts, sizes, date ranges — *before* touching anything, then asks: import
(all, a selection, or skip), install the always-on watcher (macOS LaunchAgent;
includes the web viewer at :8787), **schedule a nightly backup** (a second
question — *where should backups go?* — that installs the daily backup →
verify → restore-drill pipeline to a disk you name), wire the MCP servers into
detected clients (the `claude` CLI, or it prints the JSON block for any other
client). Every choice is skippable and persists in `<home>/config.json`; a
disabled source stays disabled across every ingest path. Re-running
`thread_archive` shows status; `thread_archive setup` revisits the choices.
Non-interactive (agents, scripts): `thread_archive --yes` accepts every
default — without `--yes`, a non-TTY run only prints guidance and never
ingests.

Skipped the watcher? Still covered: setup-generated MCP entries explicitly set
`THREAD_ARCHIVE_MCP_INGEST=1`, opting `archive-mcp` into **lazy catch-up
ingest**. A background pass at startup and (throttled) around tool calls imports
whatever landed in your local AI-tool stores since the last pass. A bare
`archive-mcp` invocation without that setting is fully read-only. On a fresh
archive give the first pass a minute to chew before expecting search hits; the
watcher install (`thread_archive setup`, or `archive daemon install`) is the
always-fresh upgrade. With the daemon installed, opted-in MCP passes degrade to
no-op lock probes — exactly one process ingests at a time, however many clients
are open.

## CLI

Two commands, split setup from operations. **`thread_archive`** is the setup
wizard (discover → consent → import → watcher → backup → MCP wiring), then
the status view; `thread_archive setup` re-enters the flow. **`archive`** is
the operator seam:

```bash
archive import <path>     # import a transcript / provider store
archive providers         # list registered providers (built-in + installed plugins)
archive watch             # watch local AI-tool stores and import incrementally
archive reindex           # rebuild index.db from the JSONL truth directory
archive migrate           # migrate older truth, then reindex and verify
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
                          #   watcher (default), --mcp the shared server, --backup the nightly
                          #   pipeline (`daemon install --backup --dest <path> [--at HH:MM]`), or
archive self-update       # explicitly fast-forward this clone to the newest eligible release tag
                          #   (the watcher checks daily; --check also reports without applying)
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
  _knowledge/       # knowledge-layer data plane: KgEvent fold + SQL topic reads
  _watcher/         # local-source watcher (self-feeding ingest)
  _mcp/             # the library-native read MCP server
  _web/             # read-only viewer: stdlib server + built bundle (cohosted by `watch --web`)
  _launchd.py       # `archive daemon`: generates + loads the watcher / MCP / nightly-backup LaunchAgents (macOS)
  _thread_import/   # vendored provider parsers (a clean, dependency-free island)
  _providers/       # the provider registry: built-in descriptors + plugin discovery
  provider/         # PUBLIC: the plugin API a third-party provider is written against
frontend/           # the viewer's React+Vite source (dev-only; builds into _web/static/)
host/               # operator layer: Makefile over `archive daemon`, family-manifest writer
scripts/            # operator tools (coverage gate, retrieval eval)
tests/install/      # isolated Docker install test + fixtures
```

## Stability

The public API is exactly three things:

- **the retrieval MCP tools** — `thread_search` and `thread_read`, served by
  `archive-mcp`;
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

Everything else is private support machinery and may change without notice:
the `thread_archive` and `archive` CLIs, the web
viewer, and every other Python module. More surface gets exposed
deliberately as it matures. `tests/test_public_api.py` ratchets the boundary.

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
  check produce per-source *degradation verdicts* (`archive coverage` prints
  them; the MCP search notice prepends a one-liner naming the remedy the next
  time you search, which is the moment you care).
- **Preservation doesn't wait for the fix.** A degraded source's recently
  active raw files are snapshotted into `dumps/drift/<source>/` — bounded,
  incremental, never auto-deleted — so a fix that comes months later can still
  recover everything the provider has since pruned.
- **The user's own agent writes the fix.** `archive fix-import <provider>`
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
  re-fires and the fix re-runs against the new core). `archive fix-import
  <provider> --pin` keeps yours forever. Every lifecycle step is audited in
  `patch-log.jsonl`, and `archive providers` shows `patched` / `patched
  (pinned)` state.

## Not supported

The scope is deliberately narrow. These are design decisions, not gaps waiting
on a release:

- **More than one machine — and merging archives.** An archive belongs to one
  Mac. Thread and event ids are locally minted and live *inside* the
  truth layer: they are the JSONL filenames, they sit in every record, in the
  append-only curatorial log, and in the inline `[e12345]` citations stored
  summaries carry. Two archives grown independently therefore occupy the same
  id space with nothing to tell them apart, and cannot be combined. There is no
  merge tool, no sync, and no federated search across archives. *Moving* an
  archive to another machine is supported — carry the directory, or
  `archive restore <mirror> --to <home>`; running two and reconciling them
  later is not.
- **Anything but macOS.** macOS is the supported platform: the daemons are
  LaunchAgents, the file locks are Unix. The Python core happens to import and
  pass its suite on Linux (public CI runs there), but the always-on pieces —
  watcher, scheduled backup, MCP LaunchAgent — do not exist off macOS, and no
  other platform is tested end to end or supported.
- **More than one user.** No accounts, no authentication, no per-user scoping.
  The web viewer binds to `127.0.0.1` and assumes whoever reaches it owns
  everything in the archive.
- **Live capture of web chats.** claude.ai, ChatGPT, and xAI arrive from account
  exports you download by hand. The self-feeding path is the local agent
  harnesses.
- **Driving a conversation.** The archive preserves and retrieves. It never
  writes back to a harness store and never sends a message.

## Web viewer

The always-on watcher cohosts a local search + reader UI: `archive watch --web`
(the shipped LaunchAgent passes it) serves at `http://127.0.0.1:8787` — a stdlib
HTTP server handing out a pre-built React bundle plus a few JSON endpoints, in
the watcher's *own* process. One process, one SQLite engine — the viewer reads
concurrently with the watcher's writes, which WAL makes safe (`_store/_base.py`).
No second daemon, and no standalone `web` verb: the viewer exists where the
persistent URL is.

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

## What it does

- **Ingests 9 agent harnesses** into one event model — Claude Code, Codex, Grok,
  Antigravity, Cowork, cloth (transcript line-streams) and Cursor, OpenCode, Claude
  Science (SQLite scanners). Web chats (claude.ai, ChatGPT, xAI) are the one manual
  path: drop a downloaded account export into `<home>/dumps/` (picked up on the next
  ingest pass) or run `archive import-export <path>` — a redrop merges, importing only
  what grew. Imports are idempotent, atomic, and survive a full reindex losslessly. A
  `cc-exthost` watcher also recovers mid-turn Claude Code steering messages that never
  reach the session JSONL.
- **Takes provider plugins**, so a harness archive has never heard of can be preserved
  without a fork. A provider is one descriptor — where its transcripts live, how to
  read them, what its format looks like — declared against the public
  `thread_archive.provider` API and found through a `thread_archive.providers` entry
  point. The built-in providers are built from that same API, so a plugin is a
  first-class source: same poll loop, same off switch, same coverage reporting.
  `archive providers` lists what is registered; see [docs/providers.md](docs/providers.md).
- **Self-feeds** — the watcher tails local stores and ingests incrementally; events land
  in the JSONL truth *before* their commit (no checkpoint in the hot loop). Zero-daemon
  freshness comes from the setup-generated MCP catch-up opt-in; a one-command macOS
  LaunchAgent upgrade (`archive daemon install`) makes it always-fresh. Neither mode
  needs an external service.
- **Declares itself** — the installer writes a small discovery manifest
  `<home>/product.json` (`host/write-manifest.py`; `make install-agent` runs
  it): name, version, data paths, and the viewer URL when the watcher serves
  one, so sibling tooling can find the archive by enumeration. The writer is
  the reference for its shape; harmless when nothing consumes it.
- **Searches locally** — FTS5 lexical (boolean / phrase / pipe-OR / code-identifier),
  optionally fused with local semantic vectors and a cross-encoder re-rank; plus
  transcript reconstruction for reading. An empty query **browses**: one row per
  thread by last activity, under the same time/source/type filters — orientation
  ("what happened yesterday") without guessing keywords. Ranked results come one
  row per thread — repeats and cross-thread duplicate content fold into
  annotations instead of spending result slots (`group='none'` for every hit).
- **Rebuilds losslessly** — `rm index.db && archive reindex` reconstructs the entire
  index from the JSONL truth; a `cp`/`rsync` of the truth dir *is* the backup.
- **Checks for releases** — provider formats drift, and a parser fix only matters if it
  reaches the machines that need it. The watcher fetches release tags daily and reports
  when the newest tag has soaked for 48 hours; applying it is the explicit
  `archive self-update` operation: checkout → reinstall → smoke check → daemon restart,
  rolling back if the new install doesn't stand up. It never touches a tree with local
  changes, and never crosses a truth-format bump without
  `--allow-format-bump`. Scheduled checks can be disabled with
  `{"update": {"enabled": false}}`; operators who deliberately want unattended apply
  can set `{"update": {"auto_apply": true}}`. Wheel installs are untouched.
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
- **Curatable** — an event-sourced topic graph (see below),
  built by a curating agent, which also stores each conversation's
  search-first summary: a few dense sentences indexed into the default search scope
  and embedded for the semantic arm, plus a structured `indexed_summary`
  (event-anchored markdown) for long threads, served by
  `thread_read summary='short'|'indexed'`.

## MCP

One server, two explicit process modes. **`thread-archive`** (`archive-mcp`)
serves the read-only `thread_search` / `thread_read` tools. The process is also
read-only by default. Setting `THREAD_ARCHIVE_MCP_INGEST=1` opts it into local
lazy catch-up ingest, throttled and cross-process-safe via the ingest-owner
lock. Curation writes deliberately have no MCP surface in this package. Client
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

The shared HTTP LaunchAgent is read-only by default too. Install it with
`archive daemon install --mcp --mcp-ingest` only when it should own catch-up;
leave the flag off when the watcher already owns ingestion.

## Knowledge layer (the topic graph)

There are exactly two kinds of thread: imported **conversations** and curated **topics**
(`thread_type='topic'`). A topic is modeled as a thread on purpose — so the graph's edges
(`thread_links`) and message→topic citations (`topic_messages`) reference one id space.
The archive keeps the **data plane** only: the event log, its fold, and the SQL topic
reads (`_knowledge/`). The analytics over it — PageRank, Leiden communities, bridges,
peers, the relevant-subjects search lens, the topic pages and hierarchy — are
[thread-librarian](https://github.com/ellamental/thread_archive_librarian)'s. The archive's base install carries the shared
community spine (Leiden) and its own corpus-native embedding graph — the
ranking signal that needs no curation — while the curated-graph analytics and
their scipy/pagerank stack are the librarian's.

Existing records stay readable as a compatibility surface: `thread_read` on a topic id
renders the topic's curated page (description, links, cited quotes — each quote anchored
to open via `around_event`) and `thread_search(topic_id=…)` scopes a search to the
topic's member conversations. With thread-librarian installed alongside, search headers
also name the subjects a result set clusters under, and topic pages regain their graph
metadata; the browseable topic surfaces (tree, communities, garden queues) are the
librarian MCP's tools.

Curation is **event-sourced**. Every graph write (`create_topic`, `link_threads`,
`add_topic_evidence`, `merge_topics`, …) appends a `KgEvent` to an
append-only `truth/kg_events.jsonl` and folds it into the SQLite projection in one
transaction. The log is the source of truth for curation; `thread_links` / `topic_messages`
are rebuildable from it — `reindex` replays the log (idempotent upsert + tombstone) to
reconstruct them, so an unlink/merge/archive is recorded history, never silent loss.

A curating agent works a **review queue** — event-bearing conversations still
missing either half of the per-thread output, held back while a thread is still
ingesting. Per thread the curator writes
**~3+ topic citations** and a **stored summary**: a short,
dense `summary` that immediately becomes a thread-meta search doc (the default search
scope is user + title + summary, and the embed cohost picks it up for the semantic
arm), plus an event-anchored `indexed_summary` for long threads. A conversation is
done once it has both a citation/link and a summary. Summaries are deliberately *not*
kg events: they're thread metadata like the title, made durable by the thread's own
latest-wins truth record, which keeps redaction's thread-meta scrub the single place
summary content ever needs erasing.

## Similar and related projects

Preserving and searching AI conversation history is a crowded space, and a lot
of the work in it is good. If thread-archive isn't what you want, one of these
probably is.

**Session search over local agent transcripts** — the nearest neighbors, all
local-first, all reading the same harness stores:

- [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) —
  Rust, the widest provider coverage in the space and the closest retrieval
  stack to this one: BM25, local ONNX embeddings, rank fusion, and a
  cross-encoder rerank over an append-only store. Its MCP surface is
  inter-agent messaging; search is CLI and TUI.
- [ctx](https://github.com/ctxrs/ctx) — Rust, many harnesses into local SQLite,
  a read-only MCP server, session replay with windowing, and read-only SQL over
  the index. Lexical retrieval, tuned for spending few tokens.
- [deja-vu](https://github.com/vshulcz/deja-vu) — Go single binary; MCP `recall`
  and `blame` tools, optional semantic search against a local Ollama or LM Studio
  endpoint, credential redaction at index time. Its
  [format registry](https://github.com/vshulcz/deja-vu/tree/main/docs/registry)
  documents each harness's on-disk shape and quirks, and is the best public
  reference for these formats.
- [episodic-memory](https://github.com/obra/episodic-memory) — TypeScript;
  copies transcripts into its own archive so search outlives harness pruning,
  local embeddings, MCP `search` and `read`. Claude Code and Codex.
- [synty](https://github.com/superlinked/synty) — Rust; a login-time tracker
  daemon, append-only JSONL as its corpus, a rebuildable index, late-interaction
  retrieval, and emergent topic clustering. Architecturally the closest cousin.
- [agentsview](https://github.com/kenn-io/agentsview) — Go; a local SQLite
  archive with full-text and optional semantic search, a web dashboard, and
  token/cost analytics. Operator-facing rather than agent-facing.
- [Agent Sessions](https://github.com/jazzyalex/agent-sessions) — a polished
  native macOS browser across many harnesses. Reads in place: a viewer, not a
  store.
- Smaller and sharper: [ccrider](https://github.com/neilberkman/ccrider) (TUI
  plus an MCP search server), [claude-historian](https://github.com/Vvkmnn/claude-historian-mcp)
  (an MCP server that deliberately keeps no index at all),
  [threadlens](https://github.com/moinulmoin/threadlens) (a lexical index that
  is explicitly disposable), and the transcript renderers
  [claude-code-log](https://github.com/daaain/claude-code-log) and
  [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts).

**Multi-provider chat archives.** [MyChatArchive](https://github.com/1ch1n/mychatarchive)
comes at the same premise from the consumer side: ChatGPT, Claude, and Grok
exports alongside local Claude Code and Cursor sessions, in one SQLite archive
with full-text search, embeddings, and MCP.

**Agent memory layers** — [mem0](https://github.com/mem0ai/mem0),
[Zep / Graphiti](https://github.com/getzep/graphiti),
[Letta](https://github.com/letta-ai/letta),
[Cognee](https://github.com/topoteretes/cognee),
[supermemory](https://github.com/supermemoryai/supermemory),
[Basic Memory](https://github.com/basicmachines-co/basic-memory) — solve an
adjacent problem: distilling conversation into facts, entities, or a knowledge
graph small enough to sit in context. They are complements rather than
alternatives. They optimize for a short, high-signal context; an archive
optimizes for keeping everything. Most also record conversations that flow
*through* them, rather than ingesting a harness's own store after the fact.

**Prior art outside AI.** [notmuch](https://notmuchmail.org/) is the
architectural precedent: immutable mail files that are never modified, all
mutable state in an index regenerable from them at any time, proven over
decades and millions of messages. [Piler](https://www.mailpiler.org/) is the
compliance-archive analogue — immutable storage, tamper verification, retention
policy.

**How thread-archive differs.** Most tools here treat the harness's own files
as the record and their index as a cache over it. Archive treats preservation
as the product: its own append-only truth log, backup with restore drills and
integrity verification, crypto-shredding redaction that stays reversible, and
unmodeled provider fields preserved verbatim so a format change costs fidelity
instead of data. The retrieval stack and topic graph are built to be read by an
agent mid-conversation rather than browsed by a person. Where these projects
lead: broader provider coverage, platforms beyond macOS, and more mileage.

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
