# Upstream fixes for the Linux-testing findings — 2026-07-23 (PR #2)

Source-tree fixes for the findings logged in
[FindingsOnInstall7.23.26.md](FindingsOnInstall7.23.26.md) (#1, #4, #5) and
[FixImportClaudeCode7.23.26.md](FixImportClaudeCode7.23.26.md) (#6), done on the
PR #2 branch. **Every change is platform-neutral** — this is a macOS-built
project gaining Linux support, and nothing here forks behavior by platform:
the parser/scaffold changes are pure Python data + template edits, the conftest
change only *removes* env vars (unset on macOS → no-op), and the doc change is
the Ubuntu-only install doc.

## Changes

### Finding #4 — `tests/conftest.py`: scrub the XDG `*_DIRS` search paths
Added `XDG_CONFIG_DIRS` and `XDG_DATA_DIRS` to the existing XDG scrub loop.
Desktop Linux sessions inject real-home entries into them (KDE:
`~/.config/kdedefaults` in `XDG_CONFIG_DIRS`; flatpak:
`~/.local/share/flatpak/...` in `XDG_DATA_DIRS`), which tripped
`tests/meta/test_isolation.py::test_no_env_var_aims_at_the_real_machine` on a
stock Kubuntu desktop. Dropped, they fall back to the spec's system defaults.
macOS: both are normally unset — popping them changes nothing.

**Verified:** full suite green (`exit 0`) on Kubuntu **without** the previous
`env -u XDG_CONFIG_DIRS` workaround.

### Finding #5 — model the drifted claude-code fields (the proper fix)
The same fix the LinuxPatch plugin carried, now in the source tree where it
belongs:

- `src/thread_archive/_thread_import/parsers/config/base.py` —
  `CLAUDE_CODE_CONFIG.known_line_fields` gains `sessionKind` + `session_id` on
  both `user` and `assistant`, plus `interruptedMessageId` and
  `classifierMetaLines` on `user` (all observed in real 2.1.x transcripts;
  sets kept sorted per the file's convention).
- `src/thread_archive/_thread_import/parsers/claude_code.py` —
  `_USER_LINE_ANNOTATIONS` / `_ASSISTANT_LINE_ANNOTATIONS` gain the matching
  mappings (`session_kind`, `session_id`, `interrupted_message_id`,
  `classifier_meta_lines`), because ledgering alone would silence the warning
  but drop the values at the builder seam. `session_id` is annotated rather
  than dropped because it is **not** a `sessionId` duplicate: on a
  resumed/forked session it carries the ORIGIN session's id (lineage) —
  verified against real samples during the fix-import repair.

**Verified:** importing the obfuscated drift fixture
(`LinuxPatch/session-drift.jsonl`) through the plain built-in importer into a
scratch archive → 9 events, **0 validation-drift records**, and the four new
annotations persisted onto the right events (`user_message_sent`,
`api_request_completed`, `tool_execution_completed`). Torn final line skipped
as designed. Historical events keep their values under
`annotations["unmodeled"]`; events parsed after this fix carry first-class
annotations.

### Finding #6 — `src/thread_archive/_repair/scaffold.py`: advisory records are not drift
The scaffold's generated `test_no_validation_drift_on_fixtures` now filters out
ledger records whose findings are all `(advisory)`-suffixed (the version
first-sighting tripwire). Previously any fixture carrying a `version` field
failed the generated test in a fresh tmp archive — on macOS and Linux alike —
which forced fixtures to omit `version` (the workaround baked into
`LinuxPatch/session-drift.jsonl`).

### Finding #7 (live-discovered) — `pr-link` / `agent-name` line types
After the watcher restarted onto the finding-#5 fix, the drift ledger kept
growing by exactly one class of record: `Unmodeled source line type 'pr-link'`
— a line Claude Code writes when a PR is opened from a session (it fired the
moment PR #2 was created *in the session doing this work*). A full-ledger
survey then showed `agent-name` (a subagent's display-name bookkeeping) as the
only other unmodeled line type. Both are session-state bookkeeping, so both
were added to `CLAUDE_CODE_CONFIG.expected_unmodeled_line_types` — preserved
verbatim as hidden records, no longer reported as drift. With these, every
non-advisory finding class in this machine's ledger is accounted for.

**Verified:** full suite green after the change; post-restart the ledger stopped
growing under live ingest (see Machine state below).

### Finding #1 — `claude-install-ubuntu.md`: venv precondition
The apt preconditions now install `python3-venv` (Ubuntu ships `python3`
without `ensurepip`, so the doc's `python3 -m venv .venv` failed out of the
box), and the no-sudo `uv` fallback used during the install test is mentioned,
including its provisioned-CPython caveat.

## Effect on the LinuxPatch plugin, and machine state

The plugin patch is **superseded** by the finding-#5 source fix: on this
machine the editable install means every newly started process already runs the
fixed parser, so activating the plugin is not needed — and the scaffold was
**cleaned up** on 2026-07-23: `~/.thread/archive/plugins/claude-code/` removed
(it held copies of private session samples; originals remain in `~/.claude`)
and the disabled plugin declaration removed from the archive home's
`config.json` (file deleted once empty — it existed only for the scaffold
entry). `LinuxPatch/` in the repo is kept as the worked example of the
fix-import repair flow.

Machine activation checklist, all done 2026-07-23:
- watcher restarted onto the fixed parser (`systemctl --user restart
  thread-archive-watcher`), restarted again after the finding-#7 addition;
- drift ledger verified flat under live ingest after the restarts;
- the per-session MCP server picks the fix up automatically on the next
  session launch (it is spawned from this clone's venv per session);
- `coverage` may keep reporting `degraded (validation_drift)` until the last
  pre-fix ledger records age out of its 7-day window — expected, no action.

## CI triage (post-push)

PR #2's CI came back red on `python (3.12)`, `python (3.14)`, and `frontend` —
**all pre-existing, none from this PR's changes**: main's own CI shows the
identical failure signature, including on a docs-only commit. Two causes:

- **Ruff lint** — import-sort (I001) and one E741 (`l` as a variable name) in
  the `_mine/` subsystem and its tests, introduced by the gold-gate/refactor
  work on main. Fixed in this PR after merging `origin/main` into the branch:
  `ruff check . --fix` (3 autofixes) plus renaming `l` → `line` in
  `tests/test_mine_orchestration.py:343`. `ruff check .` now passes clean
  (ruff 0.15.22), and the full suite + package lane stay green with main's
  new tests included.
- **Mypy** (surfaced once ruff passed) — 3 errors in main's new pool-cache
  work, none in this PR's files: `pool_cache.py` copied hits with `dict(h)`,
  erasing the `EventHit` TypedDict type (fixed with `h.copy()`, which keeps
  it — behaviorally identical shallow copy), and `retrieve_pool` declared
  `thread_id: int | str | None` while passing it to `search_events`
  (`str | None`) — `search()` resolves every ref to the ULID before calling,
  so the annotation was narrowed to the real contract (`Optional[str]`;
  verified `search()` is the only caller). `mypy`: 166 files, no issues.
- **Frontend coverage thresholds** — lines 89.72% vs 91% required, statements
  86.53% vs 88%. Pre-existing on main, entirely outside this PR's scope
  (no frontend file touched); needs real frontend test work in a follow-up.

Also observed during triage: **main was force-pushed** (`1d0f94c...20a21b4`),
which removed the direct-pushed commit `1d0f94c` (repair log + LinuxPatch
artifacts) from main's history. No content is lost — that commit is in this
PR branch's ancestry, so merging PR #2 restores it to main.

## Test evidence

- `pytest tests/ -q` → exit 0, no XDG workaround, on Kubuntu / Python 3.14
  (uv-provisioned venv, lexical-only install).
- Scratch-archive end-to-end import of the drift fixture: 0 drift records,
  annotations verified (see finding #5 above).
- macOS not run here (no macOS machine in this environment) — CI's macOS lane,
  if present, is the cross-check; changes were reviewed for platform effects
  and none forks on platform.
