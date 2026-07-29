# CLI

One namespaced command. **`thread-archive setup`** runs the wizard (discover →
consent → import → watcher → backup → MCP wiring); **`thread-archive status`**
is the status view. **`search`** and **`read`** are retrieval — the MCP tools at
a terminal.

What gets typed stays flat. Everything else acts *on* something — a source, the
index, a backup, a service agent — and lives under that noun:

```bash
# retrieval
thread-archive search [query]   # the thread_search tool: filters by time, source, tool, content type,
                                #   file (--path) or commit; no query browses recent threads;
                                #   --page walks the matched threads, --output linkable emits JSON
thread-archive read <id>        # the thread_read tool: replay a thread (--mode user|chat|full|last|ends,
                                #   --summary files, --around-event <id> to open a search hit);
                                #   takes a ULID, a legacy integer id, or a provider session uuid
thread-archive web              # open the cohosted viewer (the watcher serves it)

# ingest
thread-archive watch            # watch local AI-tool stores and import incrementally
thread-archive source list      # registered providers (built-in + installed plugins)
thread-archive source import <path>          # import a transcript / provider store
thread-archive source import-account <path>  # import a downloaded claude.ai / ChatGPT / xAI export
thread-archive source mirror    # mirror raw harness stores into <home>/source-mirror
                                #   (verbatim, gzip; nothing ever deleted)
thread-archive source coverage  # capture-coverage check: source stores reconciled against the archive
thread-archive source loads     # load progress: the in-flight load and recent runs, by phase
thread-archive source ingest    # what ingest cost over a window (--hours, default 24): per
                                #   source, and where the time went by stage
thread-archive source fix <provider>  # scaffold an override patch for a drifted import

# the index — everything rebuildable from the JSONL truth
thread-archive index rebuild    # rebuild index.db from the JSONL truth directory
thread-archive index embed      # embed user/text events still missing a vector (incremental
                                #   catch-up; --rebuild re-embeds everything)
thread-archive index migrate    # migrate older truth, then rebuild and verify
thread-archive index verify     # integrity check: truth parses + matches the index
thread-archive index repair     # quarantine damaged truth lines; restore committed content from the index

# durability
thread-archive backup run <dest>      # mirror the truth dir (hardlink generations under <dest>/.generations)
                                      #   + the recovery bundle under <dest>/.recovery (config, retained
                                      #   exports, health/ledger snapshots)
thread-archive backup nightly <dest>  # the scheduled pipeline: backup → verify (age-gated escalation)
                                      #   → restore drill → coverage
thread-archive backup drill <dest>    # prove the backup restores: rebuild an index from the mirror
                                      #   + smoke read/search
thread-archive backup restore <mirror> --to <home>  # actually restore: staged rebuild + verify, then
                                      #   atomic publish (--generation <stamp> picks a retained
                                      #   snapshot; --list-generations shows them)

# this machine
thread-archive status           # archive health / counts / last verify + backup + drill + coverage outcomes
thread-archive service <action> # install/uninstall/restart/status a service agent (launchd on macOS,
                                #   systemd --user on Linux) — the always-on watcher (default), --mcp
                                #   the shared server, --backup the nightly pipeline
                                #   (`service install --backup --dest <path> [--at HH:MM]`)
thread-archive self-update      # packaged installs only: install the newest release from PyPI — operator-
                                #   driven, nothing updates on its own (--check reports without installing;
                                #   a clone updates with git, a uv/pipx install with its own upgrade verb)
thread-archive uninstall        # remove this machine's archive machinery — agents, MCP wiring, manifest,
                                #   heartbeat, install record; the conversations are never touched
                                #   (--dry-run reports, --yes skips the confirmation)
```

Every pre-group spelling still resolves — `reindex`, `nightly <dest>`,
`daemon install`, `backup <dest>` and the rest run exactly what they always did.
They are listed nowhere: what keeps working is a machine already wired to them,
not a second documented way to type a verb.

**Every verb here is supported surface** (see [stability.md](stability.md)):
the CLI is the process seam the service agents, cron, operator scripts and
fingers drive, and a machine already wired to a verb cannot follow a rename.
What a verb is called and what flags it takes is the contract; what it *prints*
is not, except where a flag names a machine-readable shape
(`search --output linkable`).

`search` and `read` carry a second promise on top: they are the same
implementation `archive-mcp` serves, so a query typed here and the same query
asked mid-conversation return the same answer. The third door onto the same
archive is the web viewer, cohosted by `thread-archive watch --web`.
