# host/ — the live-ingest LaunchAgent (operator flow)

`archive watch`, packaged as a macOS LaunchAgent. This is what makes the archive
**self-feeding**: it polls the local AI-tool stores and imports new conversation
content the moment it lands — no external service feeds the archive, and nothing
has to be migrated in after the fact.

The LaunchAgent itself is installed by the package — `archive daemon install`
(`src/thread_archive/_launchd.py` generates and loads the plist; no template
here). This directory is the **operator layer on top**: the Makefile wraps the
daemon verb and additionally writes the thread-family manifest
(`write-manifest.py`, which needs the repo checkout and never ships).

Unlike a service-based watcher that POSTs each changed session path to a backend
ingest route, this one runs entirely local: it calls the importer directly
(`thread_archive._importers`) and writes straight to the JSONL truth + SQLite index.
No backend, no peer mesh, no health server — just change-detect → import.

The plist runs `archive watch --web`, so this always-on process also **cohosts the
read viewer** (http://127.0.0.1:8787) in the same process — giving the viewer and
the archive-link endpoint a persistent URL without a second daemon. The web reader
runs concurrently with the watcher's writes; WAL makes that safe (`_store/_base.py`).

## What it watches

The nine providers with importers, at their default home-dir locations:

| provider       | store                                                        |
|----------------|--------------------------------------------------------------|
| claude-code    | `~/.claude*/projects/**/*.jsonl` (+ `*/subagents/*.jsonl`)   |
| codex          | `~/.codex/sessions/**/*.jsonl`                               |
| grok           | `~/.grok/sessions/**/chat_history.jsonl`                     |
| antigravity    | `~/.gemini/antigravity-cli/brain/**/transcript.jsonl`       |
| cloth          | `~/.cloth/threads/*.jsonl` (Claude-Code-shaped harness)      |
| cursor         | Cursor `state.vscdb` (SQLite, mtime-gated)                   |
| opencode       | `~/.local/share/opencode/opencode.db` (SQLite, WAL-gated)    |
| cowork         | `~/Library/Application Support/Claude/local-agent-mode-sessions/` |
| claude-science | `~/.claude-science/orgs/*/operon-cli.db` (SQLite, WAL-gated) |
| cc-exthost     | VS Code exthost log — recovers lost Claude Code steering msgs |

A `(mtime_ns, size)` fingerprint skips unchanged files, so an idle system is a
no-op. Thread identity is `(source, source_id)` and re-reads are idempotent on
each event's `dedup_key`, so re-importing an already-seen file adds nothing.

**Steering recovery (`cc-exthost`).** Mid-turn steering messages typed into the
Claude Code VS Code extension *while the model is streaming* reach the model but are
never written to the session JSONL — so the JSONL sources silently drop them (~5% of
typed messages, disproportionately decisions). They *are* recorded in VS Code's
extension-host log. This source reads that log, resolves each webview message to its
thread (the channel's cwd + resumed session id), and imports only the ones whose uuid
is absent from the session JSONL (a 5-minute grace lets the JSONL claim anything still
mid-write first). It runs last, after the JSONL sources, so it only has the genuinely
lost messages left to capture.

## Durability — no checkpoint in the hot loop

Each import commits its own transaction, and the truth-log seam flushes every
event (and every new thread's metadata record) to its `threads/<id>.jsonl` file
**before** that COMMIT. So an imported event is durable in the JSONL the instant
it's in SQLite — nothing waits for a checkpoint.

The watcher runs only a **cheap maintenance** pass (`checkpoint(snapshots=False)`)
on a slow cadence (default 300 s), and only when something was imported since the
last one: it rebalances the shard layout if the archive crossed ~16k threads and
advances the manifest watermark. It does **not** rewrite the cross-thread overlay
snapshots (`thread_links.jsonl` / `topic_messages.jsonl`) — conversation ingest
never changes those (they're projections of the append-only `kg_events` curatorial
log), so there's nothing to rewrite.

## Vector freshness — the live embed cohost

Lexical (FTS5) index aside, semantic search needs each user/text event **embedded**
into `event_vectors`. Ingest keeps the FTS index current, but embedding is a separate
step — so without upkeep the vector arm falls behind live ingest, and recent threads
become findable by keyword but **not by meaning** (a paraphrased query that relies on
the semantic arm misses them).

So the watcher cohosts an **incremental embed** on the same slow cadence as
maintenance (`--embed-interval`, default 300 s): each pass embeds the freshest
user/text events still missing a vector (anti-join, newest-first), bounded by
`--embed-batch` (default 512) so a backlog drains over cycles without stalling the
loop. The embed backend loads once and stays warm in this process; it no-ops without
the `[embeddings]` extra, and a failure is logged, never fatal. `--no-embed` disables
it. One process keeps both index arms current — no second daemon.

For a one-shot catch-up (e.g. after a long gap or a fresh `[embeddings]` install),
`archive embed` fills the whole vector gap immediately; `archive embed --rebuild`
re-embeds everything.

## Using the archive MCP from Claude Science (a *Local command* connector + grants)

Claude Science (the AI Workbench app) can run the **same stdio `archive-mcp`** — no HTTP,
no extra daemon — but it spawns connectors in a **sandbox**, so two things must be true.

What *doesn't* work: a **Remote** (URL) connector. Claude Science's `safeFetch` is an SSRF
guard — it rejects every loopback/private host (`127.0.0.0/8`, `10/8`, `192.168/16`, …, and
bare `localhost`), so a connector can never point at a local server. Local is the only path.

The connector sandbox is `(allow file-read*)` then `(deny file-read* (subpath $HOME))` —
everything outside `$HOME` is readable, all of `$HOME` is denied **unless granted**. So a
home-resident `archive-mcp` needs read grants for the three `$HOME` paths it touches:

| grant (read-only)              | why                                              |
|--------------------------------|--------------------------------------------------|
| `~/dev/thread/archive`         | the repo: the venv **and** the editable `src/`   |
| `~/.pyenv`                     | the interpreter + `libpython` + stdlib (pyenv build) |
| `~/.thread/archive`            | the archive data (`index.db`, truth log)         |

Grants are persistent "host access" mounts (`host_grants` table): add them from the app's
**Permissions** panel, or ask the Science agent to grant filesystem access to those paths
(it calls `request_host_access` → you approve the card). Then in **Connectors → add a
*Local command* connector** pointing at `~/dev/thread/archive/.venv/bin/archive-mcp` and
**Reconnect**. (Verified by replicating the seatbelt profile with `sandbox-exec`: with the
three grants, `thread_search` returns live results; the optional embed/re-rank models stay
disabled under the sandbox, which only degrades semantic ranking — FTS is unaffected.)

The default stdio wiring (Claude Code, Cursor) is unchanged; this is purely a Claude
Science connector + grants, needing no code here.

## Install

```bash
cd host
make install-agent     # `archive daemon install` + write the family manifest
make logs              # tail
make status            # is it loaded? pid?
make restart           # after a code edit
make uninstall-agent
```

(Outside the monorepo, `archive daemon install` alone is the whole install —
the Makefile's only addition is the family manifest.)

Requires the `archive` console script in the repo venv (`pip install -e .` at the
repo root). Logs go to `~/.thread/archive/logs/`. The agent writes to the default
archive home (`~/.thread/archive`); set `THREAD_ARCHIVE_HOME` in the plist's
`EnvironmentVariables` to point it elsewhere.
