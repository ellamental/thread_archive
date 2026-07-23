# LinuxPatch — claude-code fix-import repair (SUPERSEDED by the upstream fix)

> **Status 2026-07-23 (later the same day):** the proper fix landed in the source
> tree on this same PR branch — see
> [../LinuxTesting/UpstreamFixes7.23.26.md](../LinuxTesting/UpstreamFixes7.23.26.md)
> (finding #5). With the editable install, every newly started process already runs
> the fixed parser, so **activating this plugin is no longer needed**. The plugin is
> harmless if activated anyway (its reach-ins are idempotent unions that now add
> nothing). This directory is kept as a worked example of the `fix-import` repair
> flow on Linux.

The completed 2026-07-23 `fix-import claude-code` repair, originally held here pending
operator go-ahead. The scaffold at `~/.thread/archive/plugins/claude-code/` remains the
pristine template and the live archive is unmodified (no `--activate`, `config.json`
still `enabled: false`).

Full session log — diagnosis, wiring analysis, test results, revert record:
[../LinuxTesting/FixImportClaudeCode7.23.26.md](../LinuxTesting/FixImportClaudeCode7.23.26.md)

## Contents

| file | what it is |
|---|---|
| `patch_claude_code.py` | the finished patch module — drop into `~/.thread/archive/plugins/claude-code/` |
| `session-drift.jsonl` | the obfuscated test fixture — goes to `.../plugins/claude-code/fixtures/` |

## Verification status

**5/5 green** — the scaffold's pre-wired suite (`test_patch.py` + `conftest.py`) was run
against exactly these two files in an isolated copy on 2026-07-23. The fixture already
has the `version` keys stripped (workaround for finding #6 in the session log: the
scaffold's drift test counts the version first-sighting advisory as drift, so any
fixture carrying a `version` field fails in a fresh tmp archive — upstream-actionable).

## To activate

```bash
cp LinuxPatch/patch_claude_code.py ~/.thread/archive/plugins/claude-code/
mkdir -p ~/.thread/archive/plugins/claude-code/fixtures
cp LinuxPatch/session-drift.jsonl ~/.thread/archive/plugins/claude-code/fixtures/
cd ~/.thread/archive/plugins/claude-code
env -u XDG_CONFIG_DIRS /home/brantgoe/A_Dev/thread_archive/.venv/bin/python -m pytest . -q   # expect 5 passed
env -u XDG_CONFIG_DIRS /home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive fix-import claude-code --activate
# then verify the warnings stop:
/home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive coverage
cat ~/.thread/archive/patch-log.jsonl
```

The patch is unpinned: the next `thread_archive self-update` retires it (the proper fix
should ship upstream — findings 5 and 6 are reported in `LinuxTesting/`). Pin with
`fix-import claude-code --pin` if you want to keep it across updates.
