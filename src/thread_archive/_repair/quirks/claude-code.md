# Claude Code format quirks

Operational knowledge for repairing the `claude-code` parser (also read by the
`cowork` and `claude-science` providers via `parser_id="claude-code"` — a fix
for one may need a config `derive()` for the others).

## Store shape

- `~/.claude*/projects/<project-path>/<session-id>.jsonl` — one JSONL file per
  session; `<project-path>` is the cwd with `/` → `-`. The `.claude*` glob is
  deliberate (handles `.claude` → `.claude1` renames).
- Subagent transcripts nest: `<project>/*/subagents/**/agent-*.jsonl`. A
  workflow directory also holds a `journal.jsonl` — a run ledger, NOT a
  transcript; it repeats per run and must never be imported under one id.
- Sessions born under the archive home are typed as hidden `system` threads —
  scheduled background jobs depend on this to keep their own transcripts out of
  the work queue.

## Line kinds and precedence

- `type: "user" | "assistant" | "system"` carry the conversation; `system`
  subtypes mark commands, file snapshots, and compaction.
- Tool results arrive as **user** messages containing `tool_result` blocks —
  they are not coalesced. Role precedence matters: a user-typed line whose
  message content is tool_result blocks is machinery, not operator prose.
- Provider versions ride on lines (`version` field); the seen-versions
  tripwire records first sightings — drift onset usually matches one.

## Compaction

- A `system` line with `subtype: "compact_boundary"` breaks the parent chain:
  `parentUuid: null`, with `logicalParentUuid` pointing at the last
  pre-compaction message and `compactMetadata.preTokens` carrying the size.
- All original messages REMAIN in the JSONL after compaction — disconnected
  from the active chain, not deleted. Orphaned parent references after a
  compaction are expected; validation warns, never errors.

## Thinking blocks

- Model-specific: required for Opus-class models, exempt for Sonnet/Haiku and
  all `agent-*` (subagent) sessions. The parser config's
  `thinking_exempt_models` is the ledger for this — a new non-reasoning model
  name showing up as a "missing thinking" finding is config drift, not parser
  drift.

## Identity and dedup

- `uuid` → provider_message_id; `sessionId` → provider_conversation_id;
  `parentUuid` → provider_parent_id; `timestamp` (ISO 8601) → created_at.
- source_id is `<project-dir-name>:<file-stem>` — the watermark and thread key.
  Nothing in a fix may change how these derive: dedup identity is downstream
  of them, and the re-import test will catch a change by duplicating events.

## Standing hazards

- **Torn final line**: the newest line of a live session is routinely
  half-written. The importer must skip it and keep complete lines; every
  fixture should include one.
- **IDE context tags** in user content (`<ide_opened_file>`, `<ide_selection>`)
  extract to `ide_context` blocks — new tag names are ledger drift, not prose.
- **Branching** exists (uuid/parentUuid tree) but is rarely exercised; the
  config declares `has_parent_references`, not full branch persistence.
- New top-level line fields and new `message.*` fields are preserved under
  `annotations["unmodeled"]` and warned about by the residual path — a fix for
  "new field" drift is usually one `derive(known_line_fields=...)` away, with
  no parser code at all.
