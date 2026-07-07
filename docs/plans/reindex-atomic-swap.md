# Plan: atomic reindex (build-and-swap)

Status: **landed 2026-07-07** — steps 1–4 plus in-process reconnect shipped in
`truth/jsonl_log.py:reindex` (pre-flight disk guard, flock quiesce shared with the
watcher's pass, build into `index.db.rebuild`, WAL fold, atomic `os.replace`, stale
sidecar removal), with the watcher holding the lock shared across each pass and
reconnecting its engine on the first pass after a skipped one
(`watcher/daemon.py:run`). Cross-process reader reconnect for the *MCP read
servers / cohosted web* (step 5's generation-marker option) remains the open
follow-up — until then those readers serve the pre-swap inode until they
reconnect or restart, and a non-watcher writer (the librarian MCP) is not
quiesced: its mid-reindex writes land in truth + the old index and appear on the
next reindex. Tests: `tests/test_truth.py` (torn-line tolerance, failed-build
leaves the old index intact, lock shared-vs-exclusive semantics).
Scope: `thread_archive` — `truth/jsonl_log.py:reindex`, `watcher/daemon.py`, `config.py`
Priority: **low** — a follow-up hardening, not a live bug. The collision/dup failure
mode is already fixed (see "Relation to the landed fix" below); this only closes the
*kill-leaves-a-partial-index* window.

## Problem

`reindex` rebuilds `index.db` from the JSONL truth by **deleting every projected
table, then bulk-loading** in per-batch transactions (`truth/jsonl_log.py`). It is
not atomic against either a crash or a concurrent writer:

- **Kill / crash mid-reindex → a partial index.** The `DELETE` commits, then the
  reload runs in many small transactions. A kill (e.g. a foreground timeout, an OOM,
  a `^C`) between the delete and the end of the load leaves the index truncated —
  search/read return incomplete results until someone re-runs reindex.
- **A live writer during reindex sees a truncated index.** WAL lets readers run, but
  the events table is genuinely missing rows mid-rebuild.

This is exactly what turned a routine reindex into an incident: repeated reindex
attempts each truncated the index and left it progressively more partial.

## What the landed fix already covers (and what it doesn't)

The shipped fix — `AUTOINCREMENT` ids (persistent `sqlite_sequence` high-water that
`DELETE` can't reset) + `INSERT OR REPLACE` loaders — makes reindex **collision-safe,
dup-tolerant, and safely re-runnable**: a writer minting ids during a reindex can
never recycle a historical id, and a duplicate PK in the truth collapses last-wins
instead of aborting. So a killed reindex is now *recoverable by re-running it*.

What it does **not** give: **atomicity**. A killed reindex still leaves a partial
index visible until the re-run. This plan closes that gap.

## Approaches considered

1. **Single giant transaction** (wrap `DELETE` + all loads in one `BEGIN…COMMIT`).
   Atomic and exclusive, but a single-transaction rewrite of the ~12 GB index
   balloons the WAL to multiple GB before commit — real **disk-full risk on the
   target hardware (a MacBook Air)**. Rejected: trades the fixed incident for a new
   one.

2. **Naive file swap** (build `index.db.rebuild`, `os.replace` over `index.db`).
   The build runs with no concurrency and a small per-batch WAL — good — but breaks
   **live open connections**: the watcher, the per-client MCP read servers, and the
   cohosted web all hold open fds to the *old* inode. After the swap they keep using
   the orphaned old file (lost writes / stale reads) until they reconnect. Needs
   reconnect coordination. **This is the basis for the plan below.**

## Proposed approach: build-and-swap with quiesce + reconnect

1. **Pre-flight.** Refuse (or warn) if free disk < size(index.db) × ~1.2. The temp
   build needs room for a second copy of the data tables (vectors/FTS rebuild, not
   copy).

2. **Quiesce the writer.** reindex takes an exclusive **`flock`** on a lock file
   (e.g. `<home>/.reindex.lock`). The watcher's `poll_once` tries the same lock
   non-blocking at the top of each pass and **skips ingest while it's held** (the
   loop already tolerates a skipped/erroring poll — `daemon.py`). `flock`
   auto-releases on process death, so a killed reindex can't wedge the watcher.
   - Keep the cohosted **web server up** during the pause — only ingest pauses, not
     the read UI. (Today the watcher hosts both; ensure the pause gates writes, not
     the HTTP server.)

3. **Build into a temp db.** reindex builds `index.db.rebuild` from truth using the
   existing loaders (per-batch commits → bounded WAL). No other process touches it.

4. **Atomic swap.** `os.replace(index.db.rebuild, index.db)` — atomic on the same
   filesystem. The temp build path must live on the same volume as `index.db`.

5. **Reconnect.** In-process: dispose the module-global engine + `reset_handles()`
   so the next `get_engine()` opens the new file. Cross-process (MCP read servers,
   web): pick one —
   - (a) an **index-generation marker** (a small file / `PRAGMA user_version` bump)
     that read paths check and re-init the engine on change, **or**
   - (b) rely on `pool_pre_ping` + connection recycle to land on the new inode,
     accepting a brief window of stale reads, **or**
   - (c) signal the watcher/servers to re-init.
   (a) is cleanest; (b) is the cheapest and may be acceptable given reads are
   idempotent. **This cross-process reconnect is the hard part and may be its own
   follow-up.**

6. **Catch up.** The watcher releases the lock, re-inits onto the new `index.db`, and
   ingests anything it skipped during the pause (replayed from `import_state` /
   source files — it never lost durability, since truth is written before commit).

## Acceptance criteria

- Killing reindex at any point leaves the **old index fully intact** — search/read
  answer uninterrupted throughout; a re-run completes cleanly.
- A live watcher across a reindex **loses no ingested events** (caught up after) and
  **never observes a partial index**.
- **No disk-full risk**: bounded per-batch WAL on the temp build + a pre-flight
  free-space guard.
- Tests:
  - reindex killed mid-build → the pre-existing index still answers a known query;
  - reindex with concurrent writes → no lost events, no partial-index window;
  - the `flock` quiesce → the watcher cleanly skips and resumes.

## Effort / risk

Moderate. The build-into-temp + swap + in-process reconnect is straightforward and
contained to `reindex`. The **cross-process reader reconnect** (step 5) is the only
genuinely tricky piece — scope it first; if it's heavy, ship steps 1–4 + in-process
reconnect (already a large improvement: kill-atomic for the writer/CLI) and treat the
MCP-reader reconnect as a separate item.
