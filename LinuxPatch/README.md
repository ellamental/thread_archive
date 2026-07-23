# LinuxPatch — claude-code fix-import repair (written, tested, NOT activated)

Working artifacts of the 2026-07-23 `fix-import claude-code` repair, preserved here
after the operator asked for a revert before activation. The scaffold at
`~/.thread/archive/plugins/claude-code/` was restored to its pristine template;
the live archive was never modified (no `--activate`, `config.json` still
`enabled: false`).

Full session log — diagnosis, wiring analysis, test results, revert record:
[../LinuxTesting/FixImportClaudeCode7.23.26.md](../LinuxTesting/FixImportClaudeCode7.23.26.md)

## Contents

| file | what it is |
|---|---|
| `patch_claude_code.py` | the finished patch module — drop into `~/.thread/archive/plugins/claude-code/` to resume |
| `session-drift.jsonl` | the obfuscated test fixture — goes to `.../plugins/claude-code/fixtures/` |

## State when work stopped

Suite was 4/5 green. The one failure (`test_no_validation_drift_on_fixtures`) is a
scaffold-test issue, not a patch bug: the test counts the *version first-sighting
advisory* as drift, so any fixture carrying a `version` field fails in a fresh tmp
archive (upstream-actionable, logged as finding #6 in the session log).

## To resume the repair

```bash
cp LinuxPatch/patch_claude_code.py ~/.thread/archive/plugins/claude-code/
mkdir -p ~/.thread/archive/plugins/claude-code/fixtures
cp LinuxPatch/session-drift.jsonl ~/.thread/archive/plugins/claude-code/fixtures/
# remove the "version" keys from the fixture lines (advisory-finding workaround),
# then:
cd ~/.thread/archive/plugins/claude-code
env -u XDG_CONFIG_DIRS /home/brantgoe/A_Dev/thread_archive/.venv/bin/python -m pytest . -q
env -u XDG_CONFIG_DIRS /home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive fix-import claude-code --activate
```
