"""JSONL truth-log: the durable source of truth for the archive.

The storage model is **JSONL-as-truth + SQLite-as-rebuildable-index**: the JSONL
directory on disk is the only authoritative store, and ``index.db`` is a pure
projection of it — ``rm index.db && archive reindex`` reconstructs the whole index
from the JSONL, and ``cp``/``rsync`` of the directory *is* the backup. Nothing
durable lives only in SQLite.

**One file per thread.** Each conversation (and each topic — topics are threads
too) is its own ``threads/<id>.jsonl``, the same shape as a Claude Code session
file: a ``{"type": "thread", ...}`` metadata record (latest wins) followed by one
``{"type": "event", ...}`` line per event, in order. A new event appends to that
one thread's file; you can open, diff, or ``cp`` a single conversation. The two
cross-thread overlays — ``thread_links`` (the topic graph's edges) and
``topic_messages`` (topic evidence) — aren't any one conversation's content, so
they stay as snapshot files (``thread_links.jsonl`` / ``topic_messages.jsonl``).

**Adaptive sharding.** A flat ``threads/`` directory is comfortable to ~16k files;
past that, tooling (file trees, ``ls``, completion) drags. ``manifest.json`` records
a ``shard_depth``: 0 = flat (``threads/<id>.jsonl``), 1 = 256 id-bucketed subdirs
(``threads/<id%256>/<id>.jsonl``, good to a few million), 2 = two levels. The path
resolver computes a thread's location from the recorded depth, so reads and writes
always agree; crossing the threshold triggers an auto-rebalance, and once sharded
every checkpoint re-homes any straggler file. The rebalance is crash-safe by
construction: the new depth is persisted *before* any file moves, a file whose home
already exists is merged (appended) rather than overwritten, and the sweep is
serialized across processes by a flock — see :func:`_maybe_rebalance`. Small
archives stay flat with zero ceremony.

Writes preserve the **JSONL ⊇ SQLite** invariant: a row is staged on the session
and flushed **and fsynced** to its thread file *before* the COMMIT it belongs to, so
the projection can never hold a row the truth lacks — the truth append meets the
same durability bar as SQLite's own WAL commit (plain ``fsync``, the same call
SQLite issues; neither uses ``F_FULLFSYNC``). Each append batch is framed by a
drain-intent undo journal, so a power loss mid-batch is rolled back to the
pre-batch baselines on the next write (see the drain-intent section).
:func:`reindex` is the recovery
primitive — it rebuilds the SQLite store from the JSONL directory as a
**build-and-swap**: the new index is built in a temp file and atomically renamed
over ``index.db``, so a killed reindex leaves the old index fully intact; a
corrupted or deleted index is never a data-loss event.
:func:`rebuild_truth_from_store` is the inverse: it re-emits the whole per-thread
truth from the current store (used once to migrate an older monolithic
``events.jsonl`` into per-thread files).
"""

# The storage model above is implemented across the _truth submodules; this
# module is the facade every consumer imports through:
#   layout      — paths, manifest, sharding, record serialization
#   locks       — the three cross-process flock families
#   drain       — append handles, staged writes, the drain + its crash framing
#   maintenance — checkpoint + shard rebalance
#   rebuild     — integrity scan, reindex, rebuild_truth_from_store
# ruff: noqa: F401

from __future__ import annotations

from . import drain, layout, locks, maintenance, rebuild
from .drain import (
    DRAIN_INTENT_FILE,
    _append_line,
    _clear_intent,
    _fsync_handle,
    _handle,
    _intent_committed,
    _intent_path,
    _recover_crashed_drain,
    _repair_torn_tail,
    _stage,
    _tail_is_intent_only,
    _undo_drain,
    _write_intent,
    append_event_row,
    append_kg_event,
    record_thread,
    reset_handles,
    unstage_thread,
    write_events,
)
from .layout import (
    _CROSS_THREAD,
    KG_EVENTS_FILE,
    MANIFEST_LOCK_FILE,
    THREADS_SUBDIR,
    TRUTH_FORMAT_VERSION,
    TruthFormatError,
    _classify_parse_errors,
    _coerce,
    _datetime_cols,
    _depth_for,
    _final_nonempty_lineno,
    _fsync_dir,
    _infer_shard_depth,
    _iter_jsonl,
    _json_default,
    _manifest_lock_path,
    _manifest_path,
    _max_dir_occupancy,
    _now_iso,
    _read_manifest,
    _row_dict,
    _shard_depth,
    _thread_file,
    _thread_relpath,
    _write_manifest,
    log_dir,
    update_manifest,
)
from .locks import (
    REBALANCE_LOCK_FILE,
    REINDEX_LOCK_FILE,
    TRUTH_WRITE_LOCK_FILE,
    _hold_reindex_lock,
    _rebalance_lock_path,
    _reindex_lock_path,
    _truth_write_lock,
    _truth_write_lock_path,
    _try_rebalance_lock,
    shared_ingest_lock,
    try_shared_ingest_lock,
)
from .maintenance import (
    _checkpoint_changed_threads,
    _checkpoint_locked,
    _maybe_rebalance,
    _merge_file_into,
    _write_snapshot,
    checkpoint,
)
from .rebuild import (
    _build_fk_violations,
    _carry_import_state,
    _committed_regression,
    _content_divergence,
    _files_for_thread,
    _fold_wal,
    _hash_key_check,
    _load_table,
    _load_thread_files,
    _parsed_equal,
    _reconcile_collapsed_citations,
    _replay_kg_events,
    _store_rows_failing_key_hash,
    _truth_units_missing_from_store,
    _unlink_build,
    emit_thread_file,
    rebuild_truth_from_store,
    reindex,
    scan_truth_counts,
    thread_file_load_order,
)
