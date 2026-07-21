# The truth storage format

This documents the on-disk format of the archive's truth directory
(`$THREAD_ARCHIVE_HOME/truth/`, default `~/.thread/archive/truth/`). The truth
directory is the **only authoritative store**: `index.db` (its sibling in the
archive home) is a pure SQLite projection of it, and `vectors.sqlite` (inside
the truth dir) is a derived embedding cache — both are rebuildable, neither is
truth. A `cp`/`rsync` of the truth directory *is* the backup; `thread_archive reindex`
reconstructs everything else from it.

The format is one half of the package's public API — the other half is the
retrieval MCP tools (`thread_search` / `thread_read`); everything else,
the `thread_archive` CLI included, is private support machinery. This document is
the durability promise: data written by one release must stay readable by
the next. The CLI's backup verbs (`backup`, `verify`, `restore-drill`,
`reindex`, `repair`) are the private enforcement machinery behind that
promise.

## Versioning

`manifest.json` carries `"version"` — the **format version**, currently **2**.
It is independent of the package version.

Version 2's shape: thread ids are **ULIDs** — 26-character Crockford base32
strings whose embedded 48-bit timestamp is the thread's start, so
lexicographic id order is chronological order and ids are globally unique
(archives merge without rewriting). A thread that ever had an integer id
carries it as `legacy_id`, a permanent alias resolvable everywhere a thread
ref is accepted. Shard buckets are derived from the sha256 of the id string
(byte *i* names the level-*i* bucket directory), not from the id's numeric
value. Version-1 archives are migrated with `thread_archive migrate`; it rewrites the
truth under the reindex lock, rebuilds `index.db`, and verifies the result
before returning success.

- The version bumps only for a change an existing reader would *misinterpret*:
  record shapes, file layout, sharding semantics. Adding an optional field to a
  record is **not** a bump — readers must tolerate unknown fields on records
  they read (though `rebuild_truth_from_store` deliberately refuses to
  *re-emit* truth carrying fields its own models don't map, so an older binary
  never lossily rewrites newer truth).
- A reader that finds a version **newer** than it supports must refuse the
  archive rather than guess (`thread_archive` raises `TruthFormatError`).
- A writer that finds a version **older** than it emits must refuse to mutate
  the archive until it has been migrated. Reads and `thread_archive reindex` remain
  available for diagnosis and recovery.
- A missing or corrupt manifest infers v1 from integer-named thread files and
  otherwise uses the current version; shard depth is inferred from the layout.

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
    <hh>/<id>.jsonl      #   shard_depth 1: hh = sha256(id)[0:2] (lowercase hex)
    <hh>/<hh>/<id>.jsonl #   shard_depth 2: sha256(id)[0:2], then sha256(id)[2:4]
  blobs/                 # content-addressed binary content (images, documents)
    <hh>/<sha256><ext>   #   hh = first two hex chars; ext from media type (.png, .pdf, .bin)
  kg_events.jsonl        # append-only curatorial event log (knowledge layer)
  thread_links.jsonl     # snapshot: topic-graph edges (rebuildable from kg_events)
  topic_messages.jsonl   # snapshot: topic evidence   (rebuildable from kg_events)
  import_state.jsonl     # snapshot: importer cursors/watermarks
  redactions.jsonl       # append-only redaction log (encrypted recovery bundles)
  vectors.sqlite         # derived embedding cache — NOT truth, safe to delete
```

## manifest.json

A single JSON object, written atomically (tmp + fsync + rename) under an
exclusive flock so concurrent writers each own their keys:

| key                  | meaning                                                       |
| -------------------- | ------------------------------------------------------------- |
| `version`            | truth format version (this spec) — currently `2`              |
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

**Thread record** — `type: "thread"` plus the thread's fields: `id` (ULID
string), `legacy_id` (int or absent — the pre-ULID integer id, kept as a
permanent alias), `name` (unique slug), `title`, `thread_type` (`"conversation"` | `"topic"`),
`description`, `search_description`, `summary`, `indexed_summary`, `source`
(provider, e.g. `"claude-code"`), `source_id`, `source_metadata` (object),
`thought_count`, `user_id`, `experiment_id`, `archived` (bool),
`exclude_from_search` (bool), `workspace`, `topic_kind`,
`epistemological_type`, `inserted_at`, `updated_at`.

**Event record** — `type: "event"` plus: `id` (int, globally unique),
`thread_id` (ULID string), `stream_id` (turn grouping), `api_call_id`, `event_type` (e.g.
`user_message_sent`, `thought_generated`, `tool_called`, `tool_returned`),
`payload` (object; the event's content — shape varies by `event_type`),
`occurred_at`, `recorded_at`, `caused_by_event_id`, `correlation_id`,
`dedup_key`.

A redacted event's `payload` is the marker envelope
`{"_redacted": {"key_id": ..., "at": ...}}` — the content is deliberately
absent from the line (see redactions.jsonl below). Readers must treat a
payload carrying the `_redacted` key as content-free: render a placeholder,
skip content-hash validation against `dedup_key` (the key still names the
*original* content — it is kept so re-import cannot resurrect the plaintext
under a fresh id).

**Blob refs.** Large binary content (a pasted screenshot's base64, a
tool-result image, an attached document) is extracted out of payloads into the
content-addressed `blobs/` directory. Wherever a payload dict carried
`{"data": "<base64>", "media_type": ...}`, the extracted form is the same dict
with `data` replaced by `"blob_hash"` (sha256 hex of the raw bytes — the blob's
filename stem under `blobs/<hh>/`) and `"blob_bytes"` (raw size); every other
key is untouched. The transform is exactly invertible: restoring `data` as the
standard base64 of the blob file's bytes reproduces the original dict, which is
how such payloads re-hash against `dedup_key` (below). Blob files are truth —
they ride every backup of the truth directory — and are immutable and shared
(the same content pasted twice is one file). Historical payloads written before
extraction may still carry inline base64 `data`; readers must accept both
forms. A redacted event's blob files are deleted unless another live event
references the same hash (see redactions.jsonl — the recovery bundle carries
the content inline).

`dedup_key` is the event's timestamp-free natural identity,
`{provider_message_id | c=<hash>}:{event_type}:{tool=…|blk=…|}:{content_hash}`
with `content_hash` = first 16 hex chars of sha256 over the payload's semantic
content keys — computed over the *inline* form, so validating a blob-extracted
payload against its key means reconstituting `data` from the blob files first.
It is bare (never thread-prefixed); uniqueness is enforced per
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

## redactions.jsonl

Append-only redaction log — recorded history, never rewritten. Two record
kinds:

- **redaction**: `type: "redaction"`, `key_id` (hex, names the key),
  `thread_id`, `event_ids`, `reason`, `redacted_at`, `alg`
  (`"AES-256-GCM"`), `nonce` (base64), `ciphertext` (base64). The ciphertext
  is the *recovery bundle* — the redacted event payloads, any citation quotes
  that carried the content, and any thread-meta fields (title/summary) that
  derived from it — AES-256-GCM-encrypted with `key_id` as associated data.
- **unredaction**: `type: "unredaction"`, `key_id`, `at` — the redaction was
  reversed (its content restored from the bundle).

The keys live in `<home>/keyring.json`, deliberately **outside** the truth
directory: a backup of truth mirrors ciphertext only. Deleting a key from the
keyring (without escrow) is crypto-erasure — the bundle is permanently
unreadable while the log keeps the redaction's shape as history.

## import_state.jsonl

Snapshot of importer cursors, one row per `(source, source_id)`: `id`,
`source`, `source_id`, `thread_id`, `last_line_count`, `last_file_size`,
`last_content_hash` (sha256 of the covered byte prefix — the proof the
imported lines are still the file's first lines), `last_message_uuid`,
`last_import_at`, `created_at`. Losing this file loses no content — the next
watcher pass re-imports and `dedup_key` collapses the duplicates.
