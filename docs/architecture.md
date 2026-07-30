# How it works

Serverless-native, single-user, single-machine: it watches this machine's
agent-harness stores and imports provider transcripts into one event model.

It is a standalone package — no hosted backend, no cloud service, no host
application it depends on. The supported interfaces are the retrieval tools
(over MCP, or as the `search` / `read` CLI verbs), the documented on-disk
format, and the provider plugin API (see [stability.md](stability.md)) — there
is no other public Python API.

## Built like a database, not a folder of exports

- Plain JSONL files are the source of truth — human-readable, greppable, yours.
  The search index is disposable and rebuilds from them at any time.
- Crash-safe writes with intent journaling, fsync discipline, and automatic
  recovery. Your history survives power loss, killed processes, and corrupted
  indexes.
- Built-in backup, integrity verification, and restore drills: recovers from
  corruption or an errant delete, and the nightly pipeline checks that the
  backup actually restores. The archive is ordinary files on disk — whatever
  backs up the rest of your data covers it the same way.

## Platform assumptions

No hosted backend, no cloud, no subscription. A background watcher keeps the
archive current; every process — the MCP server, the web viewer, the daemons —
runs locally, on your machine.

This is dev tooling for one well-provisioned workstation. macOS and Linux are
the supported platforms — the always-on daemons are launchd LaunchAgents on
macOS and systemd `--user` units on Linux (public CI runs the Linux lane; the
launchd lifecycle is verified on the maintainer's own machine, which is also
where the product has the most mileage) — and the archive is single-user,
single-machine. It assumes workstation-class headroom, too: optional semantic
search keeps a multi-GB torch model resident, a normal cost on the machine this
is for.

## Layout

```text
src/thread_archive/
  _api.py           # internal coordination layer the CLI / MCP / web call into
  cli.py            # the `thread-archive` command — every verb, incl. `setup`
  _setup/           # what the archive puts on a machine and takes back off it: the wizard
                    #   behind `thread-archive setup` (first-run setup + status), and the
                    #   `thread-archive uninstall` flow
  _config.py        # truth dir + index path resolution, config.json (source opt-outs)
  _store/           # SQLite store + schema
  _truth/           # JSONL truth log + reindex
  _ops/             # backup kit: backup/mirror + restore drill, verify tiers, nightly,
                    #   health records, the action queue + its silences (notices.py)
  _importers/       # incremental import orchestration
  _retrieval/       # FTS5 + vector search, read reconstruction, the code axis (code.py)
  _knowledge/       # storage seam for the truth format's extension region — an
                    #   external knowledge layer's records, stored and backed up
                    #   here, written and specified elsewhere (docs/format.md)
  _watcher/         # local-source watcher (self-feeding ingest)
  _mcp/             # the library-native read MCP server
  _web/             # the viewer: stdlib server + built bundle (cohosted by `watch --web`);
                      #   DEV-ONLY — excluded from the wheel, like _dev
  _service/         # `thread-archive service`: the watcher / MCP / nightly-backup agents behind a
                      #   platform registry — launchd (macOS) and systemd --user (Linux) backends
  _thread_import/   # vendored provider parsers (a clean, dependency-free island)
  _providers/       # the provider registry: built-in descriptors + plugin discovery
  provider/         # PUBLIC: the plugin API a third-party provider is written against
frontend/           # the viewer's React+Vite source (dev-only; builds into _web/static/)
host/               # operator layer: Makefile over `thread-archive service`, family-manifest writer
scripts/            # repo tooling (coverage gate, frontend-build check, license notices)
search_lab/         # the search lab (never shipped): the scoring core, quality + calibration
                      #   harnesses, corpus freezing, run ledgers — see
                      #   search_lab/README.md
tests/install/      # from-nothing install proofs: clean-container Docker + realistic
                      #   discovery-driven first run (~/.claude-style stores, macOS + Linux)
```
