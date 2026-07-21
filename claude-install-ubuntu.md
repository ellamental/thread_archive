# Installing thread-archive on Ubuntu (instructions for Claude)

You are an instance of Claude Code, running inside a fresh clone of `thread-archive`
on an **Ubuntu** machine. The human cloned the repo and asked you to install it.
Follow these steps in order. Run the commands; don't just describe them. Stop and
ask the human only at the one marked decision point.

The end state: the `archive` CLI works, the read MCP server is wired into this
project's `.mcp.json`, the archive is populated, and (optionally) an always-on
watcher is scheduled via **systemd**.

This is the Ubuntu sibling of `claude-install.md`; the only real differences are
the Python source and the always-on service (systemd, not launchd).

---

## 0. Preconditions (check, don't assume)

```bash
python3 --version               # need >= 3.14
git rev-parse --show-toplevel   # confirms you're in the clone; this is REPO_ROOT
claude --version                # you
```

**Python 3.14 is not in Ubuntu's default repos** (24.04 ships 3.12). If `python3`
is older, install 3.14 first — either the deadsnakes PPA or pyenv:

```bash
# deadsnakes route
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install -y python3.14 python3.14-venv
# then use python3.14 in place of python3 below
```

Also install the build basics (a clean install may compile a C-extension wheel):

```bash
sudo apt-get install -y build-essential git
```

## 1. Create the venv and install the package

```bash
cd "$(git rev-parse --show-toplevel)"
python3.14 -m venv .venv      # or python3 if it is already >= 3.14
.venv/bin/pip install -e .
```

Local semantic search (vectors) is optional and heavy (pulls torch). **Ask the
human once** (the decision point): lexical-only, or also embeddings?

```bash
.venv/bin/pip install -e '.[embeddings]'   # only if they want vectors
```

The base install includes the corpus-graph ranking stack (`leidenalg` +
`python-igraph` + `networkx`).

## 2. Verify the install

```bash
.venv/bin/archive --version
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
ls -l "$REPO/.venv/bin/archive-mcp"   # sanity-check the command path exists
```

`.mcp.json` is git-ignored (it's machine-specific).

## 4. Populate the archive

An empty archive has nothing to search. Two ways to get conversations in:

- **Watch local AI-tool stores** (Claude Code, Cursor, Codex, … on this machine).
  On Ubuntu these live under `~/.claude`, `~/.codex`, `~/.config/Cursor`,
  `~/.config/Code`, `~/.local/share/opencode`, etc. — archive finds them per-OS.
  ```bash
  .venv/bin/archive watch --once     # one pass; or `archive daemon install` for always-on (step 6)
  ```
- **Import an export or transcript** the human points you at:
  ```bash
  .venv/bin/archive import <path> --provider <name>   # `archive providers` lists them
  ```

Then confirm it landed:

```bash
.venv/bin/archive status            # threads / events / indexed counts
```

The SQLite index is built during import; `archive reindex` rebuilds it losslessly
from the JSONL truth if it ever looks wrong.

## 5. Restart Claude Code to load the new config

`.mcp.json` is read at startup. Tell the human to **restart their Claude Code
session in this directory**, then confirm the `thread-archive` tools
(`thread_search` / `thread_read`) are available.

## 6. Always-on: the systemd watcher (+ optional nightly backup)

`archive watch --once` is a single pass. For the live, self-feeding archive,
schedule the watcher as a **systemd user service** — the productized always-fresh
upgrade. The `thread_archive` setup wizard offers this too; either path works:

```bash
.venv/bin/thread_archive setup           # wizard: offers the watcher + nightly backup
# — or the daemon directly —
.venv/bin/archive daemon install         # installs & starts thread-archive-watcher.service
.venv/bin/archive daemon status          # ActiveState/SubState/PID, or "not loaded"
```

Install enables **linger** (`loginctl enable-linger`) so the watcher keeps running
after logout and the backup timer fires with nobody logged in. On a locked-down
box that step may need admin — if install prints a hint, run:

```bash
sudo loginctl enable-linger "$USER"
```

Check on it and read its logs (no journal needed — it writes files):

```bash
systemctl --user status thread-archive-watcher
tail -F ~/.thread/archive/logs/watcher-stdout.log ~/.thread/archive/logs/watcher-stderr.log
```

Optional nightly backup (mirror → verify → restore drill) on a systemd timer:

```bash
.venv/bin/archive daemon install --backup --dest /path/to/backups
systemctl --user list-timers thread-archive-backup.timer
```

Apply a code edit later with `archive daemon restart`; remove everything with
`archive daemon uninstall` (and `--backup` for the timer).

## Done

Report to the human: install verified (tests green), `.mcp.json` written, archive
populated (give the `archive status` counts), and — if you did step 6 — the
watcher active under `systemctl --user`.
