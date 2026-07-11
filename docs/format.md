# The truth storage format

This documents the on-disk format of the archive's truth directory
(`$THREAD_ARCHIVE_HOME/truth/`, default `~/.thread/archive/truth/`). The truth
directory is the **only authoritative store**: `index.db` (its sibling in the
archive home) is a pure SQLite projection of it, and `vectors.sqlite` (inside
the truth dir) is a derived embedding cache — both are rebuildable, neither is
truth. A `cp`/`rsync` of the truth directory *is* the backup; `archive reindex`
reconstructs everything else from it.

The format is part of the package's public surface — the package exposes no
public Python API, so alongside the CLI's promised verbs and the MCP tools
this document is the durability promise: data written by one release must
stay readable by the next. The CLI's durability verbs (`backup`, `verify`,
`restore-drill`, `reindex`, `repair`) are the enforcement half of this
promise and carry the same stability commitment as the format itself.

## Versioning

`manifest.json` carries `"version"` — the **format version**, currently **1**.
It is independent of the package version.

- The version bumps only for a change an existing reader would *misinterpret*:
  record shapes, file layout, sharding semantics. Adding an optional field to a
  record is **not** a bump — readers must tolerate unknown fields on records
  they read (though `rebuild_truth_from_store` deliberately refuses to
  *re-emit* truth carrying fields its own models don't map, so an older binary
  never lossily rewrites newer truth).
- A reader that finds a version **newer** than it supports must refuse the
  archive rather than guess (`thread_archive` raises `TruthFormatError`).
- A missing or corrupt manifest is read as version 1 with the shard depth
  inferred from the directory layout.

## Encoding

Every `.jsonl` file is UTF-8, one JSON object per line, non-ASCII kept literal
(`ensure_ascii=False`). Timestamps are ISO-8601 strings (UTC where zoned).
`null` and absent are equivalent for optional fields.

## Directory layout

```text
truth/
  manifest.json          # format version, shard depth, checkpoint watermark
  threads/               # one file per thread (conversation or topic)
    <id>.jsonl           #   shard_depth 0 (flat)
    <hh>/<id>.jsonl      #   shard_depth 1: hh = id % 256 as two lowercase hex digits
    <hh>/<hh>/<id>.jsonl #   shard_depth 2: id % 256, then (id // 256) % 256
  kg_events.jsonl        # append-only curatorial event log (knowledge layer)
  thread_links.jsonl     # snapshot: topic-graph edges (rebuildable from kg_events)
  topic_messages.jsonl   # snapshot: topic evidence   (rebuildable from kg_events)
  import_state.jsonl     # snapshot: importer cursors/watermarks
  vectors.sqlite         # derived embedding cache — NOT truth, safe to delete
```

## manifest.json

A single JSON object, written atomically (tmp + fsync + rename) under an
exclusive flock so concurrent writers each own their keys:

| key                  | meaning                                                       |
| -------------------- | ------------------------------------------------------------- |
| `version`            | truth format version (this spec) — currently `1`              |
| `shard_depth`        | `0` flat, `1` or `2` levels of hex buckets under `threads/`   |
| `last_checkpoint_at` | ISO timestamp of the last checkpoint, or `null`               |
| `hashes_baseline`    | owned by `verify --hashes` (mismatch baseline); may be absent |

Writers preserve keys they don't own; readers ignore keys they don't know.

## threads/<id>.jsonl

One conversation (or topic — topics are threads) per file: a
`{"type": "thread", ...}` metadata record followed by one
`{"type": "event", ...}` line per event, in event order. Both record kinds may
repeat; for the thread record and for events sharing an `id`, the **latest
line wins** on load. Appends are fsynced before the SQLite transaction they
belong to commits (truth ⊇ index, always).

**Thread record** — `type: "thread"` plus the thread's fields: `id` (int),
`name` (unique slug), `title`, `thread_type` (`"conversation"` | `"topic"`),
`description`, `search_description`, `summary`, `indexed_summary`, `source`
(provider, e.g. `"claude-code"`), `source_id`, `source_metadata` (object),
`thought_count`, `user_id`, `experiment_id`, `archived` (bool),
`exclude_from_search` (bool), `workspace`, `topic_kind`,
`epistemological_type`, `inserted_at`, `updated_at`.

**Event record** — `type: "event"` plus: `id` (int, globally unique),
`thread_id`, `stream_id` (turn grouping), `api_call_id`, `event_type` (e.g.
`user_message_sent`, `thought_generated`, `tool_called`, `tool_returned`),
`payload` (object; the event's content — shape varies by `event_type`),
`occurred_at`, `recorded_at`, `caused_by_event_id`, `correlation_id`,
`dedup_key`.

`dedup_key` is the event's timestamp-free natural identity,
`{provider_message_id | c=<hash>}:{event_type}:{tool=…|blk=…|}:{content_hash}`
with `content_hash` = first 16 hex chars of sha256 over the payload's semantic
content keys. It is bare (never thread-prefixed); uniqueness is enforced per
`(thread_id, dedup_key)`. `null` on pre-dedup history and non-imported events.

## kg_events.jsonl

Append-only curatorial log, `type: "kg_event"` records: `id`, `event_type`
(e.g. `topic.created`, `link.added`, `evidence.added`, tombstones like
`link.removed`), `entity_type` (`topic` | `link` | `topic_message`),
`entity_id` (natural key string), `payload` (object), `actor`,
`actor_thread_id`, `caused_by_event_id`, `correlation_id`, `occurred_at`,
`recorded_at`. This log is the source of truth for curation; replaying it in
`id` order rebuilds the two snapshot files below.

## thread_links.jsonl / topic_messages.jsonl

Cross-thread overlays, written as whole-file snapshots at checkpoint (they are
projections of `kg_events.jsonl`, kept as files so a truth-only copy restores
without a replay). One row object per line, no `type` wrapper:

- **thread_links**: `id`, `source_thread_id`, `target_thread_id`, `link_type`,
  `strength`, `created_by`, `created_by_thread_id`, `evidence`,
  `observation_ids`, `created_at`, `updated_at`.
- **topic_messages**: `id`, `topic_id`, `event_id`, `thread_id`, `quote`,
  `created_by_thread_id`, `actor`, `archived_at`, `created_at`.

## import_state.jsonl

Snapshot of importer cursors, one row per `(source, source_id)`: `id`,
`source`, `source_id`, `thread_id`, `last_line_count`, `last_file_size`,
`last_content_hash` (sha256 of the covered byte prefix — the proof the
imported lines are still the file's first lines), `last_message_uuid`,
`last_import_at`, `created_at`. Losing this file loses no content — the next
watcher pass re-imports and `dedup_key` collapses the duplicates.
