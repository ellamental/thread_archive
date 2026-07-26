# Installing thread-archive from source (instructions for Claude)

This is the **from-source** install. The packaged path is
`pip install thread-archive && thread_archive setup` and needs no agent; this
doc is for running from a clone — development, or changes not yet released.

You are an instance of Claude Code, running inside a fresh clone of `thread-archive`.
The human cloned the repo and asked you to install it. Follow these steps in order.
Run the commands; don't just describe them. Stop and ask the human only at the one
marked decision point.

The end state: the `thread_archive` CLI works, the read MCP server is wired into this project's
`.mcp.json`, and the conversation archive is populated.

---

## 0. Preconditions (check, don't assume)

```bash
python3 --version          # need >= 3.12
git rev-parse --show-toplevel   # confirms you're in the clone; this is REPO_ROOT
claude --version           # you
xcode-select -p            # C-compiler toolchain (see below)
```

If `python3` is older than 3.12, stop and tell the human — nothing below will work.

The base install's compiled dependencies (`numpy`, `cryptography`) publish wide
wheel matrices and normally need no compiler. The narrow ones are behind the
`[leiden]` extra (`python-igraph`, `leidenalg`), which `[embeddings]` pulls in —
so if you install either extra and pip has to build from source, it needs a C
compiler. On a fresh Mac that means the **Xcode Command Line Tools**; if
`xcode-select -p` printed nothing (or the install later dies with a compiler
error), install them and retry:

```bash
xcode-select --install
```

(`[embeddings]` also pulls `torch`, whose wheel availability lags new Python
releases the most — if that step can't find a wheel, it's the same class of
failure.)

## 1. Create the venv and install the package

```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m venv .venv
.venv/bin/pip install -e .
```

Local semantic search (vectors) is optional and heavy (pulls torch). **Ask the human
once** (the decision point): lexical-only, or also embeddings?

```bash
.venv/bin/pip install -e '.[embeddings]'   # only if they want vectors
```

`[embeddings]` also brings the corpus-graph ranking stack (`leidenalg` +
`python-igraph`, on top of the base `networkx`): the coherence search signal is
computed over the vector pack, so it arrives with vectors and needs no topic
graph. `.[all]` is the same set under one name. A lexical-only install has no
corpus graph to partition and wants none of it.

## 2. Verify the install

```bash
.venv/bin/thread_archive --version
.venv/bin/pip install -e '.[dev]'   # pytest ships in the dev extra, not the base install
.venv/bin/pytest tests/ -q          # must be green before you go further
```

If the suite isn't green, stop and report — do not proceed onto a broken install.

## 3. Wire up the MCP server (`.mcp.json`)

This project ships `.mcp.json.example` with the read server (`thread-archive`:
`thread_search` / `thread_read`). Generate the real `.mcp.json` with this clone's
absolute path so Claude Code can launch the venv binary:

```bash
REPO="$(git rev-parse --show-toplevel)"
sed "s|ABSOLUTE_REPO_PATH|$REPO|g" .mcp.json.example > .mcp.json
```

Then sanity-check the `command` path exists:

```bash
ls -l "$REPO/.venv/bin/archive-mcp"
```

`.mcp.json` is git-ignored (it's machine-specific).

**The clone's location is now load-bearing.** This absolute path — and the ones
in the watcher/backup service units (step below) and in self-update — is baked
into the machine, not tracked in the repo. A plain `mv` of the clone dead-ends
the MCP wiring, the daemon, and self-update at once. To relocate it, move the
directory, then re-run the MCP wiring above and `thread_archive daemon restart`
(or, cleanest, re-clone at the new path and reinstall). Tell the human this
before they pick where the clone lives.

Also write the thread-family manifest — the discovery record other thread
products glob for (harmless if none are installed):

```bash
"$REPO/.venv/bin/python" "$REPO/host/write-manifest.py" --no-web
```

(Drop `--no-web` if you also install the live-ingest LaunchAgent from `host/`
later — `make install-agent` rewrites the manifest with the :8787 viewer
declared.)

## 4. Populate the archive

An empty archive has nothing to search. Two ways to get conversations in:

- **Watch local AI-tool stores** (Claude Code, Cursor, Codex, … on this machine):
  ```bash
  .venv/bin/thread_archive watch --once     # one pass; or `thread_archive daemon install` for always-on
  ```
  (`.mcp.json.example` explicitly sets `THREAD_ARCHIVE_MCP_INGEST=1`, so even
  without either, that configured `archive-mcp` cohosts a lazy catch-up pass at
  startup and around tool calls. A bare invocation without the setting is
  read-only.)
- **Import an export or transcript** the human points you at:
  ```bash
  .venv/bin/thread_archive import <path> --provider <name>   # `thread_archive providers` lists them: claude-code, codex, grok, antigravity, cloth, cowork, claude-science, cursor, opencode, …
  ```

Then confirm it landed:

```bash
.venv/bin/thread_archive status                    # threads / events / indexed counts
.venv/bin/thread_archive search "<something you discussed>"   # retrieval, right now
```

(`search` and `read` are the `thread_search` / `thread_read` tools at a
terminal — same implementation, so a hit here is a hit over MCP once Claude
Code is restarted with the config below.)

The SQLite index is built during import; if it ever looks wrong, `thread_archive reindex`
rebuilds it losslessly from the JSONL truth.

## 5. Restart Claude Code to load the new config

`.mcp.json` is read at startup. Tell the human to **restart their Claude Code session
in this directory**, then confirm the server connected (the `thread-archive` tools —
`thread_search` / `thread_read` — should be available).

## Done

Report to the human: install verified (tests green), `.mcp.json` written, and the
archive populated (give the `thread_archive status` counts). Also point them at
`.venv/bin/thread_archive setup` for the always-on upgrades this flow doesn't cover:
the watcher LaunchAgent and the nightly backup pipeline.
