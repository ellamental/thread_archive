# Install test findings — `claude-install-ubuntu.md` — 2026-07-23

Fresh-clone install test of `thread-archive` 0.0.6 following `claude-install-ubuntu.md`,
driven by Claude Code as the doc intends. The install completed end-to-end with **zero
blocking conflicts**; five findings below, two of them upstream-actionable (#4, #5).

## Host

| | |
|---|---|
| OS | Kubuntu (KDE desktop), Linux 7.0.0-28-generic |
| System Python | 3.14.4 (doc floor is 3.12) |
| Venv Python | CPython 3.14.5 (uv-managed, see finding 1) |
| Package | thread-archive 0.0.6, editable install |
| Extras | `[dev]` installed; `[embeddings]` deliberately skipped (lexical-only test) |

## Findings

### 1. `python3 -m venv` fails out of the box — `python3.14-venv` not installed
The doc's step 1 (`python3 -m venv .venv`) fails on a system where the matching
`python3.X-venv` apt package isn't present (no `ensurepip`). The doc's precondition
section installs `build-essential git` but never mentions the venv package.

- **Workaround used:** `uv venv` + `uv pip install -e .` — no sudo needed, worked cleanly.
- **Suggested doc fix:** add `python3-venv` (or `python3.X-venv`) to the apt preconditions,
  or mention `uv` as the no-sudo fallback.
- Side effect worth noting: `uv` provisioned its own CPython 3.14.5 rather than using the
  system 3.14.4. Harmless here, but the interpreter tested is not the system one.

### 2. Embeddings decision point — worked as documented
The doc's single decision point (lexical-only vs `[embeddings]`/torch) is clear. We chose
lexical-only. No issues; noted only for completeness of what this test covered.

### 3. Python 3.14 — newer than the doc assumes, no fallout
Doc floor is 3.12. On 3.14 every dependency resolved as a prebuilt wheel, including the
corpus-graph stack (`leidenalg`, `python-igraph`, `networkx`). Zero compilation, zero
conflicts.

### 4. Test-suite hermeticity guard trips on KDE's ambient `XDG_CONFIG_DIRS` (upstream-actionable)
`tests/meta/test_isolation.py::test_no_env_var_aims_at_the_real_machine` fails on a stock
Kubuntu desktop because the session exports
`XDG_CONFIG_DIRS=/home/<user>/.config/kdedefaults:...`, which points at real machine
state. The suite's conftest scrubs other XDG variables but not `XDG_CONFIG_DIRS`.

- **Impact:** step 2's "must be green before you go further" gate fails on any KDE desktop
  through no fault of the install.
- **Workaround used:** `env -u XDG_CONFIG_DIRS .venv/bin/pytest tests/ -q` → suite green
  (0 failures, only skips).
- **Suggested fix:** scrub `XDG_CONFIG_DIRS` in `conftest.py` alongside the variables it
  already clears.

### 5. Importer format drift vs current Claude Code transcripts (upstream-actionable)
Both `watch --once` and the installed watcher log recurring parse-validation warnings on
current Claude Code session files:

```
Unmodeled source line field 'assistant.sessionKind' — a new field on a known line type; ...
Unmodeled source line field 'assistant.session_id' — ...
Unmodeled source line field 'user.sessionKind' — ...
Unmodeled source line field 'user.session_id' — ...
```

- **Impact:** non-fatal — import succeeds and the values are preserved under the anchor
  event's `annotations['unmodeled']`, exactly as the drift mechanism intends. But the
  fields (`sessionKind`, `session_id` on `user`/`assistant` lines) are now standard in
  Claude Code transcripts, so every pass over an active machine emits these warnings.
- **Suggested fix:** model or ledger the two fields for the `claude-code` importer.

## End state reached

- CLI verified: `thread-archive 0.0.6`; test suite green (with the finding-4 workaround).
- `.mcp.json` generated from the example with the clone's absolute path; repo stayed git-clean.
- Archive populated via `watch --once`: 19 threads, 3,930 events, 1,919 indexed. A second
  pass confirmed idempotent ingest (27 incremental events only).
- `daemon install` succeeded: `thread-archive-watcher.service` active as a systemd user
  unit, linger already enabled (no sudo prompt needed), live imports confirmed within
  seconds, logs written to `~/.thread/archive/logs/`. Nightly backup timer not exercised.
