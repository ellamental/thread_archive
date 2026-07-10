# Changelog

## 2026-07-10 — third-pass integrity review: retry, fsync, watermark, FTS parity

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
