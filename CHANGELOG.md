# Changelog

## 2026-07-10 — sixth-pass integrity review: both-layouts twins, verify cadence

A sixth review pass, focused on what happens when a thread's truth file exists
at two shard depths at once — imminent, since the live archive (16.1k threads)
is approaching the 16,384 flat→sharded rebalance threshold, and the first
post-rebalance backup would otherwise have locked the mirror into a permanently
additive both-layouts state (the ~16k stale flat copies exceed the mirror's
deletion safety bound forever).

- **Reindex loads the canonical-depth file last** (`thread_file_load_order`), so
  a stale twin — a both-layouts backup restore, a mid-migration crash — can no
  longer shadow the canonical thread record via path sort order (`threads/00/…`
  sorts before `threads/12345.jsonl`, so the flat stale copy used to win
  last-wins). The synthesized-minimal record is also guarded: it never clobbers
  a real record supplied by a twin of the same thread. Deep verify's
  winning-record pass follows the same order.
- **`scan_truth_counts` collapses twins across files**: threads count once per
  distinct stem and the id/dedup collapse runs per thread across all its files,
  so verify over a both-layouts directory matches what reindex materializes
  (previously each twin inflated `threads` and `events_effective`, breaking the
  backup `coverage` metric). `rebuild_truth_from_store`'s pre-flight now reuses
  the scan instead of duplicating the traversal.
- **The mirror deletes provably-superseded re-homed twins regardless of the
  deletion cap**: a doomed `threads/**` file whose thread has a canonical-depth
  file at both ends, with the destination copy at least as large, is deleted
  (`rehomed_twins_deleted`); everything else stays capped. And `archive backup`
  now exits nonzero when deletions were skipped, so the scheduled wrapper's
  failure notification fires instead of the condition persisting silently.
- **Manifest writers re-read before writing** (checkpoint's final stamp, the
  truth re-emit) and mutate only their own keys, so a concurrent verify run's
  `hashes_baseline` survives instead of being clobbered by a stale
  read-modify-write.
- **Verify cadence** (backup LaunchAgent, template + installed): Sundays now run
  `--deep` plus `--backup <dest>` (the mirror was never parse-scanned before —
  a backup that doesn't parse doesn't restore), and the 1st of the month adds
  `--hashes` (payload self-validation; the bit-rot check had never been
  scheduled, so its delta-trending baseline had never been primed).

Deliberately not done: an offsite/second-disk backup destination (deferred by
the operator) and F_FULLFSYNC/F_BARRIERFSYNC on the truth fsyncs (documented
parity-with-SQLite tradeoff stands).

## 2026-07-10 — drain-intent frame: power loss mid-batch can no longer leave a partial batch

Closes the last known hole in the crash story (finding #3 of the fifth-pass
review). The drain's in-process rollback already made append batches
all-or-nothing against process failure, but a power loss mid-batch left a
partial batch in the truth indistinguishable from committed records — reindex
would materialize it, and its rolled-back autoincrement ids could later be
reused for different content.

Each drain batch is now framed by an **undo intent journal**
(`<home>/.drain.intent`): the batch's files, pre-append baselines, and record
ids are written and fsynced *before* the first data append, and the intent is
emptied after the last data fsync — all inside the truth-write mutex, so a
non-empty intent is only ever observable after a crash. Recovery runs on every
acquisition of the truth-write mutex (every drain, checkpoint backstop, and
rebalance) and at the start of `reindex` / `rebuild_truth_from_store`, and rolls
the framed files back to their baselines. Two guards keep recovery on the right
side of the invariant:

- **Committed-ids check**: any of the batch's freshly-inserted event/kg-event
  ids present in the index proves the COMMIT landed (it was atomic), so a crash
  *between* the intent clear window and COMMIT keeps its records — truncating
  committed truth (index ⊃ truth) is the forbidden direction. Undecidable (no
  readable index) also keeps: an orphaned complete batch is the pre-existing,
  dedup-collapsed safe case.
- **Tail check**: everything past a file's baseline must be the framed batch's
  own records (a torn final fragment — the crash itself — is allowed); anything
  else means another writer appended after the crash, and the file is left in
  place rather than risk cutting foreign records.

This was chosen over the review's original sketch (per-record `txn` stamps + a
commit marker record) because it changes neither the record format nor reindex:
no grandfathering across 3.5M historical lines, no cross-file marker pass, no
unbounded marker growth. Cost: one small fsync per commit.

## 2026-07-10 — fifth-pass integrity review: backup can't eat truth loss, manifest self-heals

A fifth review pass, again integrity-focused (live shallow verify clean before
and after: zero drift, zero parse errors over 3.56M events). This pass took two
of the items the fourth pass left open (manifest `shard_depth` inference; backup
hardening) and a set of detection-latency gaps:

- **Backup shrink guard.** The mirror copied whenever size/mtime differed — in
  either direction — so a truncated/corrupted source truth file silently
  overwrote its last good backup copy on the next nightly run, and the trailing
  size check saw nothing (post-copy the sizes match). Append-only truth files
  (`threads/**`, `kg_events.jsonl`) whose source copy is *smaller* than the
  backup copy are now kept at the destination, counted (`shrinks_skipped` +
  sample), and reported as divergent until investigated; `--allow-shrink` is the
  deliberate override after an understood truth re-emit.
- **Pre-backup verify gate.** `archive backup` runs the shallow integrity check
  first; a failing source still mirrors (a flawed copy beats no copy) but
  additively — delete-sync is disabled so a sick source can't strip the backup —
  and the run exits nonzero, so the launchd dead-man's-switch stamp is withheld
  and monitoring fires. `--no-verify` skips the check.
- **Backup restore-drill lite: `archive verify --backup DEST`.** Parse-and-count
  the mirror with the same scan as the live truth; zero parse errors is the hard
  requirement, and `coverage` (backup effective events / live) quantifies
  staleness for trending.
- **Manifest shard-depth inference.** A corrupt or deleted `manifest.json` used
  to silently default `shard_depth` to 0 — on a sharded archive, writers would
  grow flat twins of sharded files and stale metadata could shadow fresh records
  on reindex. The depth is now inferred from the bucket-directory layout (loudly)
  whenever the manifest is unreadable. Matters soon: the live archive is at
  ~16.1k threads against the 16,384 flat max.
- **`verify` runs `PRAGMA quick_check`.** Page-level index corruption was
  invisible until a query touched a bad page; a failed check fails verify (the
  fix is `archive reindex`, but it must be *seen*).
- **Deep verify: thread-metadata parity (report-only).** Title/description/
  summary compared between the winning truth record and the index row; a
  persistent mismatch means a missed re-stage — and the next reindex would
  silently revert the index to the stale truth record.
- **`verify --hashes` keeps its own baseline.** The mismatch counts are persisted
  in `manifest.json` and each run reports the delta vs the previous run — the
  jump is the signal, and it no longer relies on operator memory.
- **`import_state` snapshots on the maintenance cadence.** The watermark
  snapshot was written only by the full (pre-backup) checkpoint, so an index
  loss could regress source cursors by up to a day; the table is small (~800 KB
  live), so the watcher's maintenance pass now keeps it minutes-fresh.
- **Two missing directory fsyncs.** `_write_snapshot` and `emit_thread_file`
  renamed without fsyncing the parent directory (unlike `_write_manifest`); a
  power loss could revert a snapshot/re-emit after the code moved on.
- **tz-aware `occurred_at` default.** `ThreadEvent.occurred_at` fell back to
  naive local `datetime.now`, which sorts wrong against the aware UTC timestamps
  every builder path stores.

Still open, deliberately not taken: off-disk backup destination (operator
decision), crash-window transaction framing for multi-record drain batches
(since closed by the drain-intent frame — see the entry above), scheduled
verify inside the watcher (the nightly backup job now effectively verifies
daily via the gate).

## 2026-07-10 — fourth-pass integrity review: vectors survive reindex, content-level verify, guarded re-emit

A fourth review pass focused on data integrity. The live archive was clean
(shallow verify: zero drift, zero parse errors); the changes below close
design-level gaps rather than live corruption:

- **A plain `archive reindex` no longer drops the semantic index.** The vector
  sidecar restore (+ orphan prune) now runs on every reindex; `--vectors` only
  controls whether new embedding runs and the sidecar is refreshed. Previously
  the entire vectors block was gated on `--vectors`, so a plain reindex swapped
  in an index with no `event_vectors` at all.
- **`archive backup` refreshes the vector sidecar.** The watcher's live cohost
  embeds into index.db only; the nightly backup now saves the sidecar first, so
  live-embedded vectors survive an index loss and ride the mirror.
  `save_vectors_sidecar` is also a no-op when the store holds zero vectors — an
  empty save never replaces a populated sidecar.
- **Deep verify compares dedup_keys, not just id membership.** For ids present
  in both stores, a `dedup_key` disagreement (`events_key_mismatch`) fails
  verify: it means an index-only mutation the truth never received (a reindex
  would rewrite it) or corruption on one side.
- **`verify --hashes`: content-level rot detection.** Every `dedup_key` ends in
  a hash of its payload's semantic content, so both stores self-validate by
  re-hashing stored payloads against their own keys — no new state. Report-only
  (in-place payload repairs leave a legitimate stale-hash baseline; the signal
  is the count jumping between runs).
- **`rebuild_truth_from_store` protects itself.** The one operation that
  overwrites truth from the projection now holds the reindex lock exclusive and
  refuses to run when the store holds fewer events than the truth's effective
  count (`force=True` overrides the pre-flight, never the lock). Previously it
  relied on the operator to hold the lock and would happily re-emit from a
  stale, partial index.
- **`archive import-export` is quiesced.** `cmd_import_export` called the
  importer without the shared ingest lock — the one writer path that could race
  a reindex. Now locked like `api.import_path`.
- **Verify reports parse-error locations.** `scan_truth_counts` returns a
  `parse_error_sample` of `path:line` entries so a future torn-line incident is
  diagnosable from the nightly log instead of a grep over 6 GB.
- **Nightly job runs `--deep` weekly.** The launchd backup job passes `--deep`
  on Sundays, and stamps `logs/last-backup-ok` on success as a dead-man's-switch
  hook for monitoring.

Still open, deliberately not taken: off-disk backup destination (operator
decision), crash-window transaction framing, manifest `shard_depth` inference.

A third review pass over the truth store found four residual holes (none had
bitten — live deep verify was clean before and after) plus a blind spot in
verify itself:

- **DB watchers retry failed scans.** `_DbScanWatcher` (cursor/opencode) stamped
  its mtime fingerprint *before* running the scanner, so a failed scan wasn't
  retried until the source DB changed again; it now fingerprints only after a
  successful scan, matching the file watchers and the claude-science watcher.
- **fsync survives handle eviction.** A truth-append batch touching more than
  `_MAX_OPEN_HANDLES` files LRU-evicted its early handles (close flushes but
  doesn't fsync) and `_fsync_handle` silently no-opped on them — the durability
  bar quietly dropped for bulk batches. Evicted files are now reopened and
  fsynced by fd.
- **Checkpoint watermark can't eat an update.** `last_checkpoint_at` was stamped
  *after* the changed-threads query; a thread whose metadata updated between the
  two fell below the stamp and was missed by every later backstop pass. The
  watermark is now captured before the query (worst case: one harmless
  latest-wins re-append).
- **Vector sidecar saves atomically.** `save_vectors_sidecar` dropped and
  rebuilt the sidecar in place; a crash mid-save cost the hours-long embed. It
  now builds a temp sidecar and renames it over the live one.
- **Deep verify covers the search surface.** `verify --deep` now checks FTS:
  orphan shadow rows (`events_fts` pointing at missing events) and a
  shadow↔FTS5 row-count mismatch fail; indexable events with no shadow row are
  reported (`uncovered_indexable_events`) but not failed, since an event with no
  extractable text legitimately has none.

Still open from this pass, not taken: manifest corruption silently defaults
`shard_depth` to 0 (infer from layout instead — becomes live-relevant when the
archive crosses the 16,384-file flat max, which is ~270 threads away), a
restore rehearsal that verifies the *backup* copy, off-disk backup destination,
and the known crash-window transaction framing.

## 2026-07-10 — dedup at the DB, deep verify, second-order hardening (review follow-up 2, thread 3716378)

A second integrity review unwound the 07-07 residue end to end. The reindex race
had lost the source watermarks; the re-poll re-imported ~22k turns as **keyed**
rows beside their **pre-dedup-era NULL-key originals** (the membership check reads
``dedup_key`` and can't match NULL), and those re-import commits went to the
orphaned index inode — so the index of record served single copies while the truth
held every affected turn twice. A dedup-key backfill had already keyed the
originals, but **in the index only** — event truth lines have no update-in-place —
so the first rebuild from truth reverted the keys and materialized the duplicates.
Fixed durably: ``backfill_recompute --collapse`` re-keys the originals and deletes
each duplicated copy (cited/lower-id survivor; the no-anchor tool-result class is
matched from the twin's own key: type + block + content hash + occurred_at), and
``rebuild_truth_from_store`` re-emits the truth keyed and single-copy, so a rebuild
now reproduces the repair instead of undoing it. 26,970 pairs collapsed; 236,599
keys backfilled. Changes:

- **Dedup is now a DB constraint.** A partial UNIQUE index on
  ``events(thread_id, dedup_key)`` (NULL-exempt) backs the importer's advisory
  membership check. The reindex loader's INSERT OR REPLACE now collapses
  same-content twins to one row (reported as ``events_collapsed``), and a live
  insert that slips past the membership check fails loudly instead of
  duplicating. Enforcement on an existing index arrives with its next reindex.
  The unused ``events.checksum`` column is dropped from the model.
- **Verify measures what a rebuild materializes.** The truth is append-only, so
  it legitimately accumulates superseded lines (re-appended ids, same-content
  twins); ``verify`` now compares the index against the collapsed
  ``events_effective`` count and reports the superseded remainder separately.
  ``verify --deep`` adds an id-level truth↔index diff below a stable id
  watermark (in-flight ingest can't false-alarm), classifies truth-only ids as
  superseded-twin (benign) vs missing (recoverable — reindex), flags index-only
  ids (the forbidden direction), and checks kg-log parity plus dangling
  link/citation/thread references.
- **One truth writer at a time.** An exclusive flock now serializes append
  batches (the before-commit drain, the checkpoint metadata backstop, the
  rebalance move loop) across processes: interleaved same-file appends can no
  longer corrupt lines, and the drain-failure truncate can no longer chop
  another writer's records.
- **Dirent durability.** Creating a truth file (and publishing a manifest)
  fsyncs the parent directory, closing the power-loss window where a fully
  fsynced new file vanishes from the directory while its SQLite commit survives.
- **Append handles validate their inode.** A truth re-emit atomically replaces
  every thread file; a writer's cached append handle then points at the unlinked
  old inode and its appends vanish while their commits survive. Deep verify
  caught this live (72 index-only events within minutes of the re-emit — the
  watcher's cache); ``_handle`` now fstat-checks the cached handle against the
  path and reopens on mismatch. The lost lines were restored by a second
  re-emit.
- **Shrunk sources rewind.** A source file smaller than its watermark was
  silently ignored forever; the importers now log it and re-import from the top
  (dedup collapses everything already held).
- **Backup guard rails.** The mirror refuses to propagate a gutted source
  (deletions bounded by a floor + fraction of the destination), holds the
  rebalance lock so a shard sweep can't strand a moved file in neither layout,
  and ends with a structural source⊆dest check (``mirror_complete``; CLI exits
  nonzero). Hook-context sidecar fallback timestamps are now timezone-aware and
  flagged ``timestamp_inferred`` like every other fabricated time.
- **Operational.** The daily backup+verify LaunchAgent is installed (dest
  ``~/Backups/thread-archive-truth`` — same disk until an external volume
  exists — with a notify-on-failure hook into the lab backend), a dated
  pre-repair truth snapshot lives at
  ``~/Backups/thread-archive-truth-pre-keyfill-20260710``, and the two dangling
  citations (topic evidence pointing at event ids from a prior index generation)
  were tombstoned and re-cited against the surviving events.

## 2026-07-10 — data-integrity hardening (review follow-up, thread 3716374)

An integrity review reproduced four loss/corruption paths in the truth store; all
are closed:

- **Watermarks survive reindex.** `reindex` previously rebuilt only truth-backed
  tables, wiping every `import_state` cursor; the next poll then adopted each
  active source at its current EOF, permanently skipping any source lines appended
  since its last import. Reindex now carries the cursors over from the previous
  index, and full `checkpoint` snapshots them to `truth/import_state.jsonl` as a
  seed for rebuilds where no previous index exists.
- **Torn-tail repair.** Opening an append handle newline-terminates a torn last
  line (crash mid-append), so the fragment can no longer consume the next valid
  event and take it down as one unparseable line.
- **All-or-nothing truth drain.** A failure partway through the before-commit
  truth flush now truncates the touched files back to their pre-drain size, so a
  failed batch leaves no partial records for reindex to resurrect (and no
  duplicate-id re-appends on retry).
- **True-mirror backups.** `backup` deletes destination files absent from the
  source (guarded on the source being a real checkpointed truth dir), so a shard
  rebalance no longer leaves a stale layout in the backup that shadows current
  records on restore.
- **Writer/reindex coordination.** All one-shot writers — `import_path`,
  `watch(once=True)`, and the librarian's own-session mutations — now hold the
  shared reindex lock (blocking variant) across truth append + commit, and
  `open_archive` disposes pooled connections when `index.db`'s inode changed, so
  long-lived processes converge on a freshly swapped index instead of writing to
  the orphaned old one.

Remaining known gaps, deliberately not taken here: crash-window transaction
framing for the truth format, DB-level uniqueness for `(thread_id, dedup_key)`,
and a deeper `verify` (checksums, FK checks, per-backup verification).
