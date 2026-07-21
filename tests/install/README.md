# Install test (isolated, Docker)

Proves thread-archive installs and works **from nothing** — a clean container, no host
venv, no ambient state — and that a real archive lifecycle runs end to end: import every
provider → rebuild the index from JSONL truth → search it back.

## When it runs

Every archive commit: the `install` row in this repo's `ci.toml` runs the script below
via thread-ci. Commit-triggered on purpose — the proof is invalidated by tree changes,
not by wall-clock. The script finds a docker daemon on its own, starting colima
headlessly when none is reachable, so no Docker Desktop is required.

To run it by hand:

```bash
tests/install/run_install_test.sh
```

This builds the image (`tests/install/Dockerfile`, context = repo root) and runs, inside
the container:

1. **the full unit suite** (`pytest tests/`) — in full isolation; and
2. **the end-to-end install check** (`e2e_check.py`) — imports a session for every
   provider (claude-code, codex, grok, antigravity, cursor, opencode, a chatgpt and
   a claude.ai account export, claude-science, cowork), reindexes, and asserts each
   provider's content is searchable.

Exit code is non-zero on any failure.

## Test against your real conversations (obfuscated)

The default corpus is synthetic and committed. To exercise the install against the real
shape/scale of your own data without committing anything private:

```bash
python tests/install/obfuscate_fixtures.py --limit 20      # reads ~/.claude, ~/.codex, …
tests/install/run_install_test.sh                          # auto-mounts the obfuscated corpus
```

`obfuscate_fixtures.py` scrubs every free-text field (deterministic per-word hashing;
structural tags + discriminators preserved so the importers still parse) and writes to
`tests/install/fixtures-real/` — which is **gitignored**. Point `--claude-dir` /
`--codex-dir` / `--grok-dir` / `--opencode-db` at your stores if they aren't in the
default locations. Obfuscation is lossy but not a hard guarantee — never commit the
output; review a sample before sharing.

## Pieces

| file | role |
|------|------|
| `Dockerfile` | clean `python:3.14-slim`, installs `.[dev]`, runs `run_in_container.sh` |
| `run_install_test.sh` | host: ensure a daemon (colima if needed), build the image + run (mounts the obfuscated corpus if present) |
| `run_in_container.sh` | container entrypoint: unit suite + e2e check |
| `make_fixtures.py` | the synthetic provider corpus (safe to commit) |
| `e2e_check.py` | import-all → reindex → search assertions (also runs on the host) |
| `obfuscate_fixtures.py` | opt-in: real local stores → obfuscated corpus (gitignored) |

`e2e_check.py` runs on the host too (no Docker), against a temp home:

```bash
python tests/install/e2e_check.py            # synthetic
python tests/install/e2e_check.py --fixtures tests/install/fixtures-real
```
