# fix-import repair log — `claude-code` drift — 2026-07-23

Follow-up to finding #5 in [FindingsOnInstall7.23.26.md](FindingsOnInstall7.23.26.md):
Claude Code added `session_id` / `sessionKind` fields to `user`/`assistant` transcript
lines and the built-in parser doesn't model them. Non-fatal — values are preserved under
`annotations['unmodeled']` — but every watcher pass logs drift warnings (65 ledger
records / 136 findings in the first week on this machine).

## What was run today (scaffold only — nothing activated)

```bash
.venv/bin/thread_archive fix-import claude-code
# → scaffold ready: ~/.thread/archive/plugins/claude-code (15 samples collected)
```

Scaffold contents (`~/.thread/archive/plugins/claude-code/`):

| file | purpose |
|---|---|
| `PROTOCOL.md` | the repair protocol, written to be handed to an agent |
| `evidence.md` | digest of drift ledgers, coverage verdict, quarantine snapshots |
| `quirks.md` | accumulated claude-code format knowledge |
| `patch_claude_code.py` | the override module — **the parse fix goes here** (unwritten) |
| `test_patch.py` + `conftest.py` | pre-wired test suite; the activation exit bar |
| `samples/` | 15 real drifted lines from this machine's `~/.claude` store |
| `fixtures/` | empty — obfuscated test fixtures get derived from samples |

No parser change is live: `config.json` has no override entry and imports still run the
built-in parser. The scaffold is inert until `--activate` passes.

## Commands to complete the fix

```bash
cd ~/.thread/archive/plugins/claude-code

# 1. Diagnose: read the protocol and evidence, inspect the drifted samples
#    (PROTOCOL.md, evidence.md, quirks.md, samples/)

# 2. Write the fix in patch_claude_code.py — smallest shape wins:
#    parser-config derive() for these field-level drifts (session_id, sessionKind)
#    beats a parser subclass. Keep PROVIDER.name == "claude-code".

# 3. Derive obfuscated fixtures into fixtures/ (structure intact, all free text
#    replaced — samples are private conversations, fixtures are lasting artifacts)

# 4. Iterate until the suite is green
/home/brantgoe/A_Dev/thread_archive/.venv/bin/python -m pytest . -q

# 5. The gate: re-runs the suite in a fresh subprocess, enables the override in
#    config.json, then ledger-driven re-import of everything the drifted parser consumed
/home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive fix-import claude-code --activate

# 6. Verify: drift warnings should stop, coverage clean, patch logged
/home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive coverage
/home/brantgoe/A_Dev/thread_archive/.venv/bin/thread_archive status
cat ~/.thread/archive/patch-log.jsonl
```

The intended driver is an agent: open Claude Code and point it at the scaffold —
"read `~/.thread/archive/plugins/claude-code/PROTOCOL.md` and do the repair".

## Repair session log — 2026-07-23 (attempted, then REVERTED on request)

A repair was written and tested, then **reverted to the pristine scaffold before
activation** (operator instruction mid-run: revert, log, do not change yet). The live
archive was never touched: no `--activate`, `config.json` still has the scaffold's
`"enabled": false`, and all test imports ran in throwaway tmp archive homes.

### Diagnosis (from `samples/`, 15 files)

- New fields on modeled line kinds: `session_id` + `sessionKind` on
  `user`/`assistant`/`attachment`/`system` lines; `interruptedMessageId` and
  `classifierMetaLines` (rare, string-valued) on `user` lines. Only `user`/`assistant`
  roles are field-checked by the ledger, so those two role sets are the fix's whole scope.
- **`session_id` (snake_case) is NOT a duplicate of `sessionId` (camelCase).** Verified in
  sample 02: a resumed/forked session's lines carry the *origin* session's id in
  `session_id` while `sessionId` holds the current session. It is lineage information —
  a plain ledger entry (conscious drop) would discard it, so the fix must annotate it.
- `sessionKind` observed value: `"bg"` (background/scheduled sessions).

### Why the scaffold's "shape 1" (config-only derive) is not enough — three wiring facts

1. `import_session_incremental` hardcodes `parser = parser or ClaudeCodeParser()`
   (`_importers/claude_code.py:317`) — a parser subclass on the descriptor never runs
   through the `claude_code_line_stream` importer.
2. The pre-wired test suite runs in an isolated tmp home with no plugin declared, so
   `PROVIDER.parser_config` never registers there; worse, `_ensure_provider_configs` →
   `registry()` → `_build()` re-registers **every** built-in's config on load
   (`_providers/__init__.py:63`), clobbering any earlier push.
3. Registration is push-based and keyed by name; the built-in `CLAUDE_CODE_CONFIG` is a
   module-level object shared by the seed dict, the built-in descriptor, and every
   re-registration.

### Fix shape that follows (written, tested, now stashed)

- Extend `CLAUDE_CODE_CONFIG.known_line_fields["user"|"assistant"]` **in place** —
  every registration path re-registers the same object, so the extension survives
  rebuild/clobber in both the test path and the live path. (Sanctioned reach-in per the
  scaffold's own docstring.)
- Extend the parser module's `_USER_LINE_ANNOTATIONS` / `_ASSISTANT_LINE_ANNOTATIONS`
  mapping tuples so the values keep flowing through the sanctioned annotations channel
  (`session_id`, `session_kind`, `interrupted_message_id`, `classifier_meta_lines`) —
  ledgering alone would silence the warning but drop the values at the builder seam.
- Keep a derived `parser_config` on `PROVIDER` for the activated path (declarative,
  content-identical).

### Test run: 4/5 green; the 1 failure is a scaffold-test issue, not the fix

`env -u XDG_CONFIG_DIRS .venv/bin/python -m pytest . -q` →
`test_no_validation_drift_on_fixtures` failed on the **version first-sighting advisory**:
a fresh tmp archive logs `First sighting of claude-code version '2.1.218' … (advisory)`
to the same ledger the test reads, so ANY fixture carrying a `version` field fails it.
The drift fields themselves produced zero findings (the fix worked); the torn final line
was skipped cleanly (1 parse-skip logged, deterministic on re-import).

**Upstream-actionable (#6, sibling of findings 4/5):** the scaffold's drift test should
ignore advisory records — or the scaffold docs should say fixtures must omit `version`.
**Local next step when resuming:** drop the `version` key from fixture lines, rerun to
green, then `fix-import claude-code --activate`.

### Revert record

- `patch_claude_code.py` restored to the scaffold template (byte-identical size);
  `fixtures/session-drift.jsonl` removed; `__pycache__`/`.pytest_cache` removed.
- `config.json` left exactly as the scaffold command created it (`enabled: false` —
  note: the *scaffold* command creates it, worth knowing on a machine that had none).
- Stash of the working patch + fixture: session scratchpad
  (`fix-import-stash/`, volatile) — canonical copies below.

### Stashed patch module (`patch_claude_code.py`)

```python
"""Override patch for the claude-code provider — scaffolded by `thread_archive fix-import`.

## The drift

Claude Code 2.1.x (first seen on this machine 2026-07-23) grew new top-level
fields on already-modeled line kinds:

- ``session_id`` (snake_case, distinct from the long-standing camelCase
  ``sessionId``) on user/assistant/attachment/system lines. Usually equal to
  ``sessionId``; on a resumed/forked session it instead carries the ORIGIN
  session's id — real lineage information, so its value must keep flowing,
  not be consciously dropped.
- ``sessionKind`` (observed value: ``"bg"``) on the same line kinds.
- ``interruptedMessageId`` and ``classifierMetaLines`` on user lines (rare).

## The fix (ledger + explicit annotation)

Two reach-ins, applied at module import so every path sees them — the plain
``parser_config`` override alone only takes effect when the provider registry
loads this plugin, which the scaffold's own test harness (isolated tmp home,
no plugin declared) never does:

1. The built-in ``CLAUDE_CODE_CONFIG.known_line_fields`` sets are extended IN
   PLACE. Every registration path re-registers that same object, so the
   extension survives re-registration instead of racing it.
2. The parser module's ``_USER_LINE_ANNOTATIONS`` / ``_ASSISTANT_LINE_ANNOTATIONS``
   mappings are extended so the new fields' VALUES ride the sanctioned
   annotations channel. Ledgering alone would silence the warning but drop
   the values at the builder seam.

Historical events keep the values under ``annotations["unmodeled"]``; events
parsed after this patch carry them as first-class annotations.
"""

from dataclasses import replace

from thread_archive._thread_import.parsers import claude_code as _claude_code
from thread_archive._thread_import.parsers.config.base import CLAUDE_CODE_CONFIG
from thread_archive.provider import builtin

BASE = builtin("claude-code")

_USER_FIELDS = frozenset(
    {"classifierMetaLines", "interruptedMessageId", "sessionKind", "session_id"}
)
_ASSISTANT_FIELDS = frozenset({"sessionKind", "session_id"})

_USER_ANNOTATIONS = (
    ("classifierMetaLines", "classifier_meta_lines"),
    ("interruptedMessageId", "interrupted_message_id"),
    ("sessionKind", "session_kind"),
    ("session_id", "session_id"),
)
_ASSISTANT_ANNOTATIONS = (
    ("sessionKind", "session_kind"),
    ("session_id", "session_id"),
)

# Reach-in 1: extend the live ledger object (idempotent — set union).
CLAUDE_CODE_CONFIG.known_line_fields["user"] |= _USER_FIELDS
CLAUDE_CODE_CONFIG.known_line_fields["assistant"] |= _ASSISTANT_FIELDS

# Reach-in 2: extend the annotation mappings (guarded — idempotent on re-exec).
for _pair in _USER_ANNOTATIONS:
    if _pair not in _claude_code._USER_LINE_ANNOTATIONS:
        _claude_code._USER_LINE_ANNOTATIONS += (_pair,)
for _pair in _ASSISTANT_ANNOTATIONS:
    if _pair not in _claude_code._ASSISTANT_LINE_ANNOTATIONS:
        _claude_code._ASSISTANT_LINE_ANNOTATIONS += (_pair,)

PROVIDER = replace(
    BASE,
    parser_config=CLAUDE_CODE_CONFIG.derive(
        "claude-code",
        known_line_fields={
            "user": set(_USER_FIELDS),
            "assistant": set(_ASSISTANT_FIELDS),
        },
    ),
)
```

### Stashed fixture (`fixtures/session-drift.jsonl`)

Six lines, obfuscated: user prose → assistant (thinking + text) → assistant `tool_use`
→ user `tool_result` (+`toolUseResult`) → user with `interruptedMessageId`/
`classifierMetaLines` → torn final line (truncated mid-JSON). All carry
`session_id` ≠ `sessionId` (resume-lineage shape) and `sessionKind: "bg"`; model
`claude-sonnet-4-5` (thinking-exempt). **Remove the `version` keys before reuse** (see
the advisory-finding issue above).

```jsonl
{"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","promptId":"prompt-0001","type":"user","message":{"role":"user","content":[{"type":"text","text":"placeholder request words"}]},"uuid":"aaaaaaaa-0000-4000-8000-000000000001","timestamp":"2026-07-23T12:00:00.000Z","session_id":"22222222-2222-4222-8222-222222222222","sessionKind":"bg"}
{"parentUuid":"aaaaaaaa-0000-4000-8000-000000000001","isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","effort":"medium","requestId":"req_000000000001","type":"assistant","message":{"id":"msg_000000000001","type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"thinking","thinking":"placeholder thinking words","signature":"placeholdersignature"},{"type":"text","text":"placeholder reply words"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":5}},"uuid":"aaaaaaaa-0000-4000-8000-000000000002","timestamp":"2026-07-23T12:00:01.000Z","session_id":"22222222-2222-4222-8222-222222222222","sessionKind":"bg"}
{"parentUuid":"aaaaaaaa-0000-4000-8000-000000000002","isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","effort":"medium","requestId":"req_000000000002","type":"assistant","message":{"id":"msg_000000000002","type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"tool_use","id":"toolu_000000000001","name":"Read","input":{"file_path":"/home/user/project/notes.txt"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":12,"output_tokens":6}},"uuid":"aaaaaaaa-0000-4000-8000-000000000003","timestamp":"2026-07-23T12:00:02.000Z","session_id":"22222222-2222-4222-8222-222222222222","sessionKind":"bg"}
{"parentUuid":"aaaaaaaa-0000-4000-8000-000000000003","isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_000000000001","content":[{"type":"text","text":"placeholder file words"}]}]},"toolUseResult":{"type":"text","file":{"filePath":"/home/user/project/notes.txt","content":"placeholder file words"}},"uuid":"aaaaaaaa-0000-4000-8000-000000000004","timestamp":"2026-07-23T12:00:03.000Z","session_id":"22222222-2222-4222-8222-222222222222","sessionKind":"bg"}
{"parentUuid":"aaaaaaaa-0000-4000-8000-000000000004","isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","promptId":"prompt-0002","type":"user","message":{"role":"user","content":[{"type":"text","text":"placeholder followup words"}]},"uuid":"aaaaaaaa-0000-4000-8000-000000000005","timestamp":"2026-07-23T12:00:04.000Z","session_id":"22222222-2222-4222-8222-222222222222","interruptedMessageId":"33333333-3333-4333-8333-333333333333","classifierMetaLines":"placeholder meta words"}
{"parentUuid":"aaaaaaaa-0000-4000-8000-000000000005","isSidechain":false,"userType":"external","cwd":"/home/user/project","sessionId":"11111111-1111-4111-8111-111111111111","version":"2.1.218","gitBranch":"main","entrypoint":"cli","requestId":"req_000000000003","type":"assistant","message":{"id":"msg_000000000003","type":"message","role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"text","te
```

## Notes / gotchas

- **Lifecycle:** the patch is temporary by default — the next `thread_archive
  self-update` retires it (the release is the proper fix's vehicle; upstream now has
  this drift reported as finding #5). `fix-import claude-code --pin` keeps it across
  updates; `--unpin` reverts.
- **Possible KDE test gotcha:** the install-test suite needed `env -u XDG_CONFIG_DIRS`
  on this desktop (finding #4). If the scaffold suite or the `--activate` subprocess
  trips the same hermeticity guard, prefix the pytest/activate commands the same way.
- **`--no-reimport`** skips step 5's re-import (not wanted here — the re-import is what
  folds the preserved `annotations['unmodeled']` values into the model).
