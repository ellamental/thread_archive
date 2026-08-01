# Install tests

Prove thread-archive installs and works **from nothing** — a clean environment, no host
venv, no ambient state — and that a real archive lifecycle runs end to end. Two
complementary proofs, sharing one synthetic corpus (`make_fixtures.py`):

- **Importer-level** (`e2e_check.py`) — import every provider by explicit path → rebuild
  the index from JSONL truth → search each provider's content back. Hand-feeds each store
  to the importer, so it isolates the importers from discovery.
- **Realistic first run** (`first_run.py`) — the path a *new user's first run* actually
  takes: a fake `$HOME` with every harness's store in its **real default location**
  (`~/.claude/projects`, `~/.codex/sessions`, the OS-correct app-data dir for
  Cursor/Cowork, …), discovered and ingested through the installed `thread-archive watch
  --once` with **no hand-fed paths** (`import-export` for the two account exports a user
  drops in by hand), then reindexed and searched back. This is the cross-OS lane: the same
  run proves discovery on macOS (`~/Library/Application Support`) and on Linux
  (`~/.config`).

## When it runs

| lane | what | where | trigger |
|------|------|-------|---------|
| `install` (`ci.toml`) | clean Docker container: full unit suite + `e2e_check.py` + `first_run.py` | Linux (container) | every archive commit, on the maintainer's local CI |
| `package` (`ci.toml` / GitHub Actions) | build the wheel, install into a clean venv, run the CLI lifecycle incl. `first_run.py` | **macOS** (maintainer's local CI) and **Linux** (GitHub Actions `package` job) | every commit |

So `first_run.py` — the realistic discovery-driven proof — runs on both OSes: inside a
clean Linux container (the `install` lane) and against a clean wheel-only venv on macOS and
Linux (the `package` lane). Commit-triggered on purpose: the proof is invalidated by tree
changes, not wall-clock; the `install` lane's script finds a docker daemon on its own,
starting colima headlessly when none is reachable, so no Docker Desktop is required.

Run them by hand:

```bash
tests/install/run_install_test.sh        # the Docker lane (unit suite + e2e_check + first_run)
python tests/install/first_run.py        # just the realistic first run, against the CLI on PATH / this venv
python tests/install/e2e_check.py        # just the importer-level check
python tests/install/first_run.py --keep # leave the fake home for inspection
```

The corpus is synthetic and committed, and it is the only corpus these lanes read. Real
conversations never enter the checkout — not even scrubbed: the importers' exposure to
genuine upstream shapes comes from the `source fix` scaffold, which collects real drifted
samples under the archive home where operator data belongs.

## Pieces

| file | role |
|------|------|
| `Dockerfile` | clean `python:3.12-slim` (the supported floor); builds the wheel, installs it + the `dev` dependency group, runs `run_in_container.sh` |
| `run_install_test.sh` | host: ensure a daemon (colima if needed), build the image + run |
| `run_in_container.sh` | container entrypoint: unit suite + `e2e_check.py` + `first_run.py` |
| `make_fixtures.py` | the synthetic provider corpus (safe to commit) — content defined once; `generate()` writes the flat layout, `realistic_layout()` writes each store in its real default location |
| `e2e_check.py` | importer-level: import-all (by path) → reindex → search assertions |
| `first_run.py` | realistic first run: `watch --once` discovery + `import-export` → reindex → search, OS-aware; also driven by the `package` pytest lane |
