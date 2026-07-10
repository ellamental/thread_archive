# Changelog

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
