# Installing thread-archive (instructions for Claude)

You are an instance of Claude Code, running inside a fresh clone of `thread-archive`.
The human cloned the repo and asked you to install it. Follow these steps in order.
Run the commands; don't just describe them. Stop and ask the human only at the two
marked decision points.

The end state: the `archive` CLI works, both MCP servers are wired into this project's
`.mcp.json`, the conversation archive is populated, and the topic graph is curated by the
librarian.

---

## 0. Preconditions (check, don't assume)

```bash
python3 --version          # need >= 3.14
git rev-parse --show-toplevel   # confirms you're in the clone; this is REPO_ROOT
claude --version           # the CLI the curation skill runs in
```

If `python3` is older than 3.14, stop and tell the human — nothing below will work.

## 1. Create the venv and install the package

```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m venv .venv
.venv/bin/pip install -e .
```

Local semantic search (vectors) is optional and heavy (pulls torch). **Ask the human
once** (decision point #1): lexical-only, or also embeddings?

```bash
.venv/bin/pip install -e '.[embeddings]'   # only if they want vectors
```

The base install already includes Leiden community detection (`leidenalg` + `igraph`).

## 2. Verify the install

```bash
.venv/bin/archive --version
.venv/bin/pytest tests/ -q          # must be green before you go further
```

If the suite isn't green, stop and report — do not proceed onto a broken install.

## 3. Wire up the MCP servers (`.mcp.json`)

This project ships `.mcp.json.example` with the two servers (read + librarian write).
Generate the real `.mcp.json` with this clone's absolute path so Claude Code can launch
the venv binaries:

```bash
REPO="$(git rev-parse --show-toplevel)"
sed "s|ABSOLUTE_REPO_PATH|$REPO|g" .mcp.json.example > .mcp.json
```

Then sanity-check the two `command` paths exist:

```bash
ls -l "$REPO/.venv/bin/archive-mcp" "$REPO/.venv/bin/archive-librarian-mcp"
```

`.mcp.json` is git-ignored (it's machine-specific). The hook + skill come from the
tracked `.claude/` dir and need no setup.

Also write the thread-family manifest — the discovery record other thread
products glob for (harmless if none are installed):

```bash
"$REPO/.venv/bin/python" "$REPO/host/write-manifest.py" --no-web
```

(Drop `--no-web` if you also install the live-ingest LaunchAgent from `host/`
later — `make install-agent` rewrites the manifest with the :8787 viewer
declared.)

## 4. Populate the archive

An empty archive has nothing to search or curate. Two ways to get conversations in:

- **Watch local AI-tool stores** (Claude Code, Cursor, Codex, … on this machine):
  ```bash
  .venv/bin/archive watch --once     # one pass; or `archive daemon install` (macOS) for always-on
  ```
  (Even without either, `archive-mcp` cohosts a lazy catch-up ingest pass at
  startup and around tool calls — the first `thread_search` after wiring the
  MCP triggers the initial import on its own.)
- **Import an export or transcript** the human points you at:
  ```bash
  .venv/bin/archive import <path> --provider <claude-code|codex|grok|antigravity|cursor|opencode>
  ```

Then confirm it landed:

```bash
.venv/bin/archive status            # threads / events / indexed counts
```

(Search lives in the MCP tools, not the CLI — once Claude Code is restarted
with the config below, `thread_search` is the smoke test for retrieval.)

The SQLite index is built during import; if it ever looks wrong, `archive reindex`
rebuilds it losslessly from the JSONL truth.

## 5. Restart Claude Code to load the new config

`.mcp.json` and `.claude/settings.json` are read at startup. Tell the human to **restart
their Claude Code session in this directory**, then confirm the servers connected (the
`thread-archive` and `thread-archive-librarian` tools should be available, e.g.
`review_queue`).

## 6. Curate the archive (the librarian)

The archive is searchable now, but the curation backlog starts full: the topic graph is
empty and threads have no stored summary until the librarian runs. Per conversation it
writes a few topic citations **and** a short search-first summary (summaries join the
default search scope, so this directly improves search). The `/librarian` skill is
gated by `.claude/hooks/librarian-gate.py` — it forces one-thread-at-a-time (each
thread cited + summarized before the next opens), so an instance can't skim the queue
without doing the work.

**Decision point #2 — ask the human** how big their backlog is and how much to curate
now (`archive status` shows the rough size). Run `/librarian` in the session — it
drains until the queue is empty, or pass a per-run cap (e.g. `/librarian 25`) and
re-run across sessions to work a large backlog down in slices. The queue is state-free
(done = the thread carries a citation + summary), so runs can stop and resume at any
point — a later run picks up only what's still undone.

## Done

Report to the human: install verified (tests green), `.mcp.json` written, archive
populated (give the `archive status` counts), and the librarian's state (run, partially
run, or deferred). If curation was deferred, tell them `/librarian` is the way to start
it.
