# host/ — the live-ingest LaunchAgent

`archive watch`, packaged as a macOS LaunchAgent. This is what makes the archive
**self-feeding**: it polls the local AI-tool stores and imports new conversation
content the moment it lands — no external service feeds the archive, and nothing
has to be migrated in after the fact.

Unlike a service-based watcher that POSTs each changed session path to a backend
ingest route, this one runs entirely local: it calls the importer directly
(`thread_archive.importers`) and writes straight to the JSONL truth + SQLite index.
No backend, no peer mesh, no health server — just change-detect → import.

The plist runs `archive watch --web`, so this always-on process also **cohosts the
read viewer** (http://127.0.0.1:8787) in the same process — giving the viewer and
the archive-link endpoint a persistent URL without a second daemon. The web reader
runs concurrently with the watcher's writes; WAL makes that safe (`store/_base.py`).

## What it watches

The seven providers with importers, at their default home-dir locations:

| provider     | store                                                        |
|--------------|--------------------------------------------------------------|
| claude-code  | `~/.claude*/projects/**/*.jsonl` (+ `*/subagents/*.jsonl`)   |
| codex        | `~/.codex/sessions/**/*.jsonl`                               |
| grok         | `~/.grok/sessions/**/chat_history.jsonl`                     |
| antigravity  | `~/.gemini/antigravity-cli/brain/**/transcript.jsonl`       |
| cloth        | `~/.cloth/threads/*.jsonl` (Claude-Code-shaped harness)      |
| cursor       | Cursor `state.vscdb` (SQLite, mtime-gated)                   |
| opencode     | `~/.local/share/opencode/opencode.db` (SQLite, WAL-gated)    |
| cc-exthost   | VS Code exthost log — recovers lost Claude Code steering msgs |

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

## Install

```bash
cd host
make install-agent     # materialize the plist + bootstrap the agent
make logs              # tail
make status            # is it loaded? pid?
make restart           # after a code edit
make uninstall-agent
```

Requires the `archive` console script in the repo venv (`pip install -e .` at the
repo root). Logs go to `~/.thread_archive/logs/`. The agent writes to the default
archive home (`~/.thread_archive`); set `THREAD_ARCHIVE_HOME` in the plist's
`EnvironmentVariables` to point it elsewhere.
