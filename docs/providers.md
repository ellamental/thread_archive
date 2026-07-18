# Writing a provider

A **provider** is one source of conversations archive can preserve: a coding
harness, a chat app's account export, anything that writes transcripts. Archive's
own are built from the same public API this document describes — there is no
private path, so anything a built-in can do, yours can.

Everything you import lives under `thread_archive.provider`. The rest of the
package is private and will change without warning.

## The shortest possible provider

If your harness writes Claude-Code-shaped JSONL — `user` / `assistant` lines,
one file per session — you are most of the way done already:

```python
from thread_archive.provider import Provider, RglobWatcher, claude_code_line_stream
from thread_archive.provider.parse import CLAUDE_CODE_CONFIG
from pathlib import Path

import_session = claude_code_line_stream("myharness")

PROVIDER = Provider(
    name="myharness",
    label="My Harness",
    watcher=lambda: RglobWatcher(
        Path.home() / ".myharness" / "sessions",
        import_session,
        lambda p: p.stem,          # the session id, from the file path
        name="myharness",
    ),
    importer=import_session,
    kind="line-stream",
    parser_config=CLAUDE_CODE_CONFIG.derive("myharness"),
    parser_id="claude-code",
)
```

That is a complete provider: incremental import, crash-safe resume, dedup,
search indexing, the operator's on/off switch, capture-coverage reporting.

## Installing it

Declare an entry point and install into the same environment as archive:

```toml
[project.entry-points."thread_archive.providers"]
myharness = "myharness_archive:PROVIDER"
```

```console
$ /path/to/archive/.venv/bin/pip install -e .
$ /path/to/archive/.venv/bin/archive providers
myharness  on   My Harness  (line-stream)
```

The watcher daemon runs from archive's own venv, so an entry point registered
there reaches ingest with no further wiring — restart the daemon and your source
is being captured.

While developing, skip packaging entirely and declare it in `<home>/config.json`:

```json
{"providers": {"myharness": {"module": "myharness_archive:PROVIDER",
                             "path": "~/src/myharness/archive-plugin/src"}}}
```

`path` goes on `sys.path` before the import. A config declaration wins over an
installed distribution of the same name, so you can point at a checkout without
uninstalling the released copy.

A plugin that raises on import is logged and skipped, never fatal. One broken
provider must not stop the others from capturing.

## The three importer shapes

`kind` says how `archive import --provider <name>` reaches your importer.

| `kind` | store shape | importer signature |
|---|---|---|
| `line-stream` | one file per session | `(path, source_id, *, session=None)` |
| `db-scan` | one live SQLite store, many sessions | `(db_path) -> DbScanResult` |
| `none` | not reachable by path alone | — its watcher drives it |

Use `none` when dispatch needs more than a path (an org id, a sibling metadata
file) or when there is no live store at all — a provider fed only by account
exports still declares itself so its threads have an identity and a label.

### A format of your own

When your transcript isn't another provider's shape, build the importer from
`line_stream_importer` — the same factory archive's own codex, Grok and
Antigravity providers are built from. You supply five callbacks; it supplies
everything that is easy to get wrong — reading the file once and parsing from
that buffer, the append proof that re-imports from scratch if bytes under the
cursor changed, thread creation and its discard when a poll turns out to hold
nothing, the skip ledger, the watermark, and the single transaction all of it
commits in.

`prepare(all_lines, path, source_id)` is the only callback handed the path and
the source id, so anything the others need about *which* session this is — a
sibling metadata file, a timestamp sidecar — is resolved there and carried on
the context it returns. (Grok's importer does exactly this: its timestamps live
in files next to the transcript.)

```python
from thread_archive.provider import line_stream_importer, assemble_events
from thread_archive.provider.parse import DefaultEventBuilder

import_session = line_stream_importer(
    "myharness",
    prepare=lambda all_lines, path, source_id: my_context(path.parent, source_id),
    has_importable_content=lambda new: any(ln.get("type") == "message" for ln in new),
    make_title=lambda all_lines, ctx: first_user_text(all_lines) or "My Harness Session",
    import_lines=lambda sess, tid, all_lines, new, ctx: assemble_events(
        sess, tid, to_messages(new), DefaultEventBuilder()
    ),
)
```

Your `import_lines` ends in `assemble_events`. Don't bypass it — dedup, stream
ids, monotonic timestamps, the truth-log write and search indexing all live
there, and each of them is load-bearing.

`to_messages` produces `NormalizedMessage` dicts. You can hand-build those
(most of archive's providers do) or subclass `ProviderParser`; subclassing buys
the block-construction helpers, not correctness.

### Account exports

To accept a downloaded export dropped into `<home>/dumps/`, add an `ExportSpec`:

```python
Provider(
    name="myharness",
    ...,
    export=ExportSpec(
        detect=lambda path: (path / "myharness-export.json").exists(),
        importer=import_myharness_export,
        label="My Harness",
        kind="myharness",
    ),
)
```

Each registered spec is offered the bundle in registry order and the first
`detect` to claim it wins, so keep `detect` cheap and don't claim a bundle you
can't import — a false claim quarantines someone else's export.

## Choosing a name

`name` is what threads are stored under. It is permanent: changing it orphans
every thread already imported under the old one. Lowercase, hyphenated, and
distinct from anything already in `archive providers`.

`source_id` matters just as much. It must be **stable across polls** — it keys
both the thread and its watermark, so an id that shifts creates a duplicate
thread and re-imports from zero. Derive it from something the harness won't
renumber: a session uuid in the filename, the directory the session lives in.
If it isn't already globally unique, prefix it (`{project}:{session}`).

If you do prefix it, declare the separator:

```python
Provider(name="myharness", ..., session_id_separators=(":",))
```

An agent usually knows a conversation by its bare session id, not by the
composite `source_id` you stored. The separator is what lets `thread_read` and
the viewer's deep links find the thread from that bare id. Leave it empty — the
default — when the `source_id` *is* the session id.

Declare it narrowly. Resolving a reference whose provider is unknown tries every
separator every provider declares, so a broad one invites someone else's uuid
resolving to your thread because it happens to end the same way.

## Declaring your format (ProviderConfig)

`ProviderConfig` is a drift ledger, not a schema — it never rejects anything.
It records what your format looks like today, so when the harness grows a new
line kind or field, the difference surfaces as a warning instead of passing
silently. That last case is the one nothing else catches: a new field on an
already-modeled line rides into `provider_data` and is dropped at the builder
seam with no error anywhere.

Sharing another provider's parser? Derive, don't restate:

```python
MYHARNESS_CONFIG = CLAUDE_CODE_CONFIG.derive(
    "myharness",
    expected_unmodeled_line_types={"myharness_meta"},
    known_message_fields={"cost"},
)
```

`derive` unions into the parent's ledgers, so you declare only your additions
and stay current with the parent as it grows. Declaring them on your own config
rather than editing the parent's matters in both directions: the parent isn't
blamed for a field it will never grow, and its ledger stays able to catch that
field appearing there for real.

Set `parser_id` to whichever parser reads your transcripts — your own name if
you bring a parser, the other provider's if you reuse one. Source identity and
parser identity are separate on purpose: a harness writing Claude Code's shape
must be *parsed* as Claude Code while being *stored, attributed and validated*
as itself. Repair and audit tooling groups by `parser_id`, so getting it right
is what keeps your provider inside those passes.

## When stored truth and readable transcript differ (RenderPolicy)

Most providers need nothing here — the events render as they are. Reach for a
`RenderPolicy` when your format is *correct as stored* and *misleading as
displayed*: a harness that wraps the operator's prompt in scaffolding, or writes
its transcript twice.

```python
from thread_archive.provider import DEFAULT_VIEW, Provider, RenderPolicy

def unwrap(content: str) -> str:
    match = PROMPT_RE.search(content)
    return match.group(1) if match else content        # no match → show everything

def block(block_type, data, rendered_text):
    if block_type == "myharness_telemetry":
        return None                                     # hide: carries no conversation
    if block_type == "myharness_search":
        return ("web search", data["raw"]["query"])     # render under a label
    return DEFAULT_VIEW                                 # not mine — reader decides

Provider(name="myharness", ..., render=RenderPolicy(user_content=unwrap, block=block))
```

A policy applies **only to your own threads**. Both readers resolve the thread's
provider first, so your quirk can never reshape another provider's turns — and
theirs can't reshape yours.

Two things to hold onto:

- It is presentation, never deletion. The payload is untouched and stays in the
  event log and the viewer's raw view. Hiding a block is a claim that the
  conversation reads better without it, not that it wasn't worth keeping.
- Degrade toward showing more. `user_content` runs on **every** user turn,
  including turns that merely quote the shape it looks for, and a wrapper that
  stops matching must fall back to the whole turn. A rewrite that can return
  less than it was given will eventually eat a message nobody meant it to.

## Testing it

Install the `testing` extra and enable archive's pytest plugin:

```python
# conftest.py
pytest_plugins = ["thread_archive.provider.testing"]
```

You get `archive_home` (a tmp archive, fully isolated from the real one) and a
golden harness:

```python
from thread_archive.provider.testing import assert_golden, init_archive, write_jsonl

def test_golden(archive_home):
    init_archive()
    path = archive_home / "s1.jsonl"
    write_jsonl(path, MY_FIXTURE)
    import_session(path, "s1")
    assert_golden("myharness", archive_home, GOLDEN_DIR)
```

A golden pins the *whole* normalized output — every event, payload and dedup
key — against a file you commit. That is the only thing that reliably catches
the way format contracts actually break: quietly. Generate it with
`UPDATE_GOLDENS=1 pytest`, then **read the diff before committing it**. A
regenerated golden nobody looked at locks in whatever regression prompted the
regeneration.

Make the fixture carry the shapes that decide preservation, not a happy path:
tool calls with their results, thinking, a block type you don't model, an
unknown field, and a torn final line (`write_jsonl(..., torn_tail=...)`). A
transcript being written *right now* ends mid-line, so that last one is the
normal case, not an edge case.

Worth testing beyond the golden: that a re-import of an unchanged session
writes nothing, that a grown session writes only what is new, and that a clean
session logs no drift warnings — a provider that warns on its own normal output
trains everyone to ignore the warnings.

## What you get for free

Declaring a provider is enough to get incremental capture with crash-safe
resume, thread-scoped dedup, full-text and semantic search, the web viewer,
`thread_search` / `thread_read` over MCP, the operator's `config.json` off
switch, capture-coverage staleness reporting, and inclusion in backup, verify
and reindex. None of it is per-provider.

## Reference

- `thread_archive.provider` — `Provider`, `ExportSpec`, `RenderPolicy` /
  `DEFAULT_VIEW`, the watcher bases
  (`RglobWatcher`, `FileSessionWatcher`, `DbScanWatcher`), importer construction
  (`line_stream_importer`, `claude_code_line_stream`), `assemble_events`, thread
  and watermark state, source reading.
- `thread_archive.provider.parse` — `ProviderParser`, `NormalizedMessage`, the
  content-block types, `ProviderConfig`, `DefaultEventBuilder`, the built-in
  parsers and configs.
- `thread_archive.provider.testing` — `archive_home`, `init_archive`,
  `assert_golden`, `normalized_truth`, `write_jsonl`.
