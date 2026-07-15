"""Backup: mirror the JSONL truth to a destination, and prove it restores.

A ``cp``/``rsync`` of the truth dir *is* the backup (index.db is rebuildable from
it), so :func:`backup` checkpoints, verifies, then mirrors incrementally — with
the guards that keep a sick source or a mid-flight rebalance from destroying the
last good copy (shrink guard, bounded delete-sync, hardlink generations).
:func:`restore_drill` is the end-to-end rehearsal: rebuild a full index from the
mirror in a throwaway home and prove it reads and searches.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .._config import resolve_paths
from .health import record_health, stamp_heartbeat
from .verify import verify

# Delete-sync sanity bound: refuse to delete more than this fraction of the
# destination's files (once past the absolute floor). A mass-wipe of the source —
# a bug emptying truth/, an accidental rm — must not propagate to the last backup;
# a legitimate mass-move (shard rebalance) re-homes files, so the copies land
# first and the stale paths deleted stay a bounded fraction only when the copy
# half of the mirror actually ran.
_MIRROR_DELETE_FLOOR = 64
_MIRROR_DELETE_MAX_FRACTION = 0.25

# Destination-side generation snapshots: hardlink copies of the mirror's state,
# taken before each backup run overwrites it. See _snapshot_generation.
_GENERATIONS_SUBDIR = ".generations"
_GEN_KEEP_RECENT = 7
_GEN_KEEP_MONTHS = 6


def external_disk_coverage(path: Path) -> Optional[str]:
    """Name the external whole-disk backup covering ``path``, or None.

    A same-filesystem mirror protects against bad writes, not disk loss — but
    the machine may already carry disk-loss protection the archive didn't set
    up (opt-in offsite is a choice, not a requirement). Where that protection
    is detectable, name it so status can report the true posture instead of
    warning about a gap that doesn't exist. Today this detects macOS Time
    Machine (``tmutil``): a configured destination with ``path`` not excluded.
    Fail-soft: any probe error reads as "not detected"."""
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return None
    try:
        dests = subprocess.run(
            ["tmutil", "destinationinfo"], capture_output=True, text=True, timeout=10
        )
        if dests.returncode != 0 or "No destinations configured" in dests.stdout:
            return None
        excluded = subprocess.run(
            ["tmutil", "isexcluded", str(path)], capture_output=True, text=True, timeout=10
        )
        if excluded.returncode == 0 and "[Included]" in excluded.stdout:
            return "Time Machine"
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _is_append_only_truth(rel: Path) -> bool:
    """True for truth files that only ever grow in normal operation: the per-thread
    files and the curatorial event log. The cross-thread overlay snapshots and
    ``import_state.jsonl`` are full rewrites and may legitimately shrink."""
    return (
        rel.parts[:1] == ("threads",) or rel.name == "kg_events.jsonl"
    ) and rel.suffix == ".jsonl"


def _atomic_copy(sp: Path, dp: Path, *, trim_to_newline: bool) -> None:
    """Copy ``sp`` over ``dp`` with no destructive window: the bytes land in a
    same-directory temp file, fsynced, then an atomic rename publishes them — a
    crash or disk-full mid-copy can never leave ``dp`` partial or destroy its
    previous good copy. ``copy2`` under the hood, so mtime rides along and the
    mirror's unchanged-skip keeps working.

    ``trim_to_newline`` (append-only truth files) drops an unterminated final
    fragment from the copy: the mirror doesn't quiesce writers, so a copy racing
    a live append can catch half a line — trimmed to the last newline, every
    published backup copy is a clean, parseable prefix of its source, and the
    full line arrives with the next run."""
    import shutil

    tmp = dp.parent / f".{dp.name}.tmp-{os.getpid()}"
    try:
        shutil.copy2(sp, tmp)
        with open(tmp, "rb+") as fh:
            size = fh.seek(0, os.SEEK_END)
            if trim_to_newline and size:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    pos, last_nl = size, -1
                    while pos > 0:
                        step = min(65536, pos)
                        fh.seek(pos - step)
                        idx = fh.read(step).rfind(b"\n")
                        if idx >= 0:
                            last_nl = pos - step + idx
                            break
                        pos -= step
                    fh.truncate(last_nl + 1)
            os.fsync(fh.fileno())
        os.replace(tmp, dp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mirror_dir(
    src: Path, dest: Path, *, delete: bool = False, allow_shrink: bool = False
) -> dict:
    """Incrementally mirror ``src`` into ``dest`` (skip files unchanged by size +
    mtime). Each file is published atomically (:func:`_atomic_copy`), preserving
    mtime so a re-run copies only what changed — the truth dir is append-mostly,
    so a periodic backup moves little.

    ``delete=True`` makes it a true mirror: destination files with no source
    counterpart are removed (and emptied directories pruned). Without it the mirror
    is additive, and a shard rebalance — which *moves* thread files to new paths —
    leaves the backup holding both layouts. Deletion is bounded: when the planned
    deletions exceed both the absolute floor and the fraction cap of the
    destination's files, they are skipped (``deletions_skipped``) — the mirror
    stays additive rather than letting a gutted source strip the backup.

    **Re-homed twins are exempt from that bound.** A rebalance moves every thread
    file at once, so the stale old-layout copies at the destination can vastly
    exceed the cap — and skipping them forever leaves the backup double-sized and
    holding records a restore must not see. A doomed ``threads/**`` file whose
    thread has a file at the source's canonical shard depth — present at *both*
    ends, with the destination's canonical copy at least as large as the stale
    one — is provably superseded (the sweep moves/merges, never truncates), so it
    is deleted (``rehomed_twins_deleted``) regardless of the cap. Everything else
    stays capped.

    **Shrink guard.** An append-only truth file (``threads/**``, ``kg_events.jsonl``)
    whose source copy is *smaller* than its backup copy means the source lost data —
    truncation, corruption, an accidental overwrite. Copying it would destroy the
    last good copy, so the guard keeps the destination file and counts the skip
    (``shrinks_skipped`` + sample); the trailing size check reports it as divergent
    until it is investigated. ``allow_shrink=True`` is the deliberate override for
    an understood re-emit (``rebuild_truth_from_store`` legitimately rewrites files
    smaller)."""
    from .._truth.jsonl_log import _fsync_dir

    copied = total = deleted = skipped = shrinks = 0
    shrink_sample: list[str] = []
    synced_dirs: set[Path] = set()
    for sp in src.rglob("*"):
        if sp.is_dir():
            continue
        rel = sp.relative_to(src)
        dp = dest / rel
        if dp.exists():
            ss, ds = sp.stat(), dp.stat()
            if ss.st_size == ds.st_size and int(ss.st_mtime) <= int(ds.st_mtime):
                continue
            if (
                not allow_shrink
                and ss.st_size < ds.st_size
                and _is_append_only_truth(rel)
            ):
                shrinks += 1
                if len(shrink_sample) < 10:
                    shrink_sample.append(str(rel))
                continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(sp, dp, trim_to_newline=_is_append_only_truth(rel))
        copied += 1
        total += dp.stat().st_size
        synced_dirs.add(dp.parent)
    for sd in synced_dirs:
        _fsync_dir(sd)  # the renames that published this pass's copies must stick
    twins_deleted = 0
    if delete:
        # The generations subtree is destination-only state (hardlink snapshots
        # of prior mirror runs) — never a deletion candidate, and never counted
        # toward the deletion cap's denominator.
        dest_files = [
            dp for dp in dest.rglob("*")
            if not dp.is_dir() and dp.relative_to(dest).parts[0] != _GENERATIONS_SUBDIR
        ]
        doomed = [dp for dp in dest_files if not (src / dp.relative_to(dest)).exists()]
        if doomed:
            twins, doomed = _split_rehomed_twins(src, dest, doomed)
            for dp in twins:
                dp.unlink()
                twins_deleted += 1
                deleted += 1
        limit = max(_MIRROR_DELETE_FLOOR, int(len(dest_files) * _MIRROR_DELETE_MAX_FRACTION))
        if len(doomed) > limit:
            skipped = len(doomed)
        else:
            for dp in doomed:
                dp.unlink()
                deleted += 1
        if deleted:
            for dp in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
                try:
                    dp.rmdir()  # only succeeds when empty
                except OSError:
                    pass
    return {
        "files_copied": copied,
        "bytes_copied": total,
        "files_deleted": deleted,
        "rehomed_twins_deleted": twins_deleted,
        "deletions_skipped": skipped,
        "shrinks_skipped": shrinks,
        "shrink_sample": shrink_sample,
    }


def _split_rehomed_twins(src: Path, dest: Path, doomed: list[Path]) -> tuple[list[Path], list[Path]]:
    """Partition planned mirror deletions into provably-superseded rebalance
    twins and everything else (see :func:`_mirror_dir`). A twin qualifies only
    when its thread's canonical-depth file exists at the source *and* the
    destination's copy of that canonical file is at least as large as the stale
    one — the rebalance sweep moves/merges whole files, so a genuine re-home can
    never leave the canonical copy smaller."""
    from .._truth.jsonl_log import THREADS_SUBDIR, _shard_depth, _thread_relpath

    depth = _shard_depth(src)
    twins: list[Path] = []
    rest: list[Path] = []
    for dp in doomed:
        rel = dp.relative_to(dest)
        if rel.parts[0] == THREADS_SUBDIR and dp.suffix == ".jsonl":
            try:
                tid = int(dp.stem)
            except ValueError:
                tid = None
            if tid is not None:
                canonical = _thread_relpath(tid, depth)
                try:
                    if (
                        canonical != rel
                        and (src / canonical).exists()
                        and (dest / canonical).stat().st_size >= dp.stat().st_size
                    ):
                        twins.append(dp)
                        continue
                except OSError:  # canonical dest copy missing/unreadable — stay capped
                    pass
        rest.append(dp)
    return twins, rest


def _snapshot_generation(dest: Path) -> dict:
    """Hardlink-snapshot the mirror's current state into
    ``<dest>/.generations/<UTC stamp>/`` — taken *before* the mirror run
    overwrites it, so each generation is the destination as the previous run
    left it. The rolling mirror alone propagates destruction: same-size
    corruption, a mistaken ``--allow-shrink``, or a bad repair overwrites the
    only other copy on the next nightly run. Generations make that recoverable.

    Hardlinks make a generation nearly free (the truth is append-mostly, and
    the mirror only ever publishes destination files via whole-file rename —
    ``_atomic_copy`` — so a snapshot's linked inodes are never mutated by later
    runs; deletions just unlink the mirror's name). One generation per run —
    every overwrite of the mirror has a pre-state snapshot, so a second (bad)
    backup on the same day can't destroy the day's only pre-state. The
    snapshot is built under a dot-tmp name and renamed into place (the gens
    dir fsynced after), so a killed run never leaves a directory that looks
    like a complete generation and a published one survives power loss.

    Retention coalesces by day, never within one: every generation from the
    newest ``_GEN_KEEP_RECENT`` distinct UTC days is kept (pruning a same-day
    sibling would discard exactly the pre-bad-run state generations exist
    for), plus the newest generation of each distinct month until
    ``_GEN_KEEP_MONTHS`` months are covered. Failure to snapshot degrades to
    the pre-generations behavior (reported, never blocks the mirror itself)."""
    import logging
    import shutil
    from datetime import datetime, timezone

    out: dict = {"generation_created": None, "generations_pruned": 0}
    if not (dest / "manifest.json").exists():
        return out  # first run: nothing at the destination to preserve
    gens = dest / _GENERATIONS_SUBDIR
    now = datetime.now(timezone.utc)
    try:
        if not gens.is_dir():  # not exist_ok=True: smbfs answers EPERM, not
            gens.mkdir()  # EEXIST, for mkdir of an existing dir
        for stale in gens.glob(".tmp-*"):  # a killed snapshot's half-built tree
            shutil.rmtree(stale, ignore_errors=True)
        names = sorted((p.name for p in gens.iterdir() if p.is_dir()), reverse=True)
    except OSError as e:  # an unusable gens dir (degraded network dest, name
        # rejected by the share) — snapshot is protection, not the backup itself
        out["generation_error"] = str(e)
        logging.getLogger(__name__).exception("backup: generation snapshot failed")
        return out
    name = now.strftime("%Y%m%dT%H%M%SZ")
    while name in names:  # two runs within a second — still one gen per run
        name += "x"
    tmp = gens / f".tmp-{name}"
    try:
        linked = 0
        for sp in dest.rglob("*"):
            if sp.is_dir():
                continue
            rel = sp.relative_to(dest)
            if rel.parts[0] == _GENERATIONS_SUBDIR:
                continue
            gp = tmp / rel
            gp.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(sp, gp)
            except OSError:  # filesystem without hardlinks — take the copy cost.
                # copyfile, not copy2: a generation is a restore source, so only
                # content matters (retention is keyed by the generation dir's
                # name), and metadata replication EPERMs on network mirrors
                # (smbfs refuses reads of system xattrs like com.apple.provenance).
                shutil.copyfile(sp, gp)
            linked += 1
        os.rename(tmp, gens / name)
        from .._truth.jsonl_log import _fsync_dir

        _fsync_dir(gens)  # the publishing rename itself must survive power loss
        out["generation_created"] = name
        out["generation_files"] = linked
        names.insert(0, name)
        names.sort(reverse=True)
    except OSError as e:  # snapshot is protection, not the backup itself
        shutil.rmtree(tmp, ignore_errors=True)
        out["generation_error"] = str(e)
        logging.getLogger(__name__).exception("backup: generation snapshot failed")
    # Coalesce by day, never within one (see docstring).
    days: list[str] = []
    for n in names:  # newest first
        if n[:8] not in days:
            days.append(n[:8])
    keep = {n for n in names if n[:8] in set(days[:_GEN_KEEP_RECENT])}
    months = {n[:6] for n in keep}
    for n in names:
        if n not in keep and n[:6] not in months and len(months) < _GEN_KEEP_MONTHS:
            keep.add(n)
            months.add(n[:6])
    for n in names:
        if n not in keep:
            shutil.rmtree(gens / n, ignore_errors=True)
            out["generations_pruned"] += 1
    out["generations_kept"] = len(keep)
    return out


def backup(
    dest: str,
    *,
    home: Optional[str] = None,
    allow_shrink: bool = False,
    verify_first: bool = True,
) -> dict:
    """Back the archive up by mirroring its JSONL truth directory to ``dest``.

    A ``cp``/``rsync`` of the truth dir *is* the backup (index.db is rebuildable from
    it), so this checkpoints first — flushing the cross-thread overlays + any thread-
    metadata updates so the on-disk truth is a complete restore set — then mirrors
    ``truth/`` into ``dest`` incrementally. Point ``dest`` at a *different disk /
    machine*: for pre-retention history the truth log is the only copy.

    The source is verified before it is mirrored (``verify_first``): a truth dir
    that fails the shallow integrity check (parse errors, index ⊃ truth drift) is
    still backed up — a flawed copy beats no copy — but *additively*: delete-sync
    is disabled for the run so a sick source can't strip the last good backup, and
    ``verify_ok=False`` in the result flags the run for investigation.

    The mirror holds the rebalance lock so a shard sweep can't move files under it
    (which could otherwise leave a moved file in *neither* layout in the backup for
    a whole cycle), and it finishes with a structural check — per-file size parity
    of every source ``.jsonl`` against its destination copy (``mirror_complete``).
    Live appends between the copy pass and the check make a file *larger* at the
    source; that isn't a mirror failure, so the check tolerates dest ≤ src growth
    on files it copied and only flags missing or divergent copies. Append-only
    truth files are additionally shrink-guarded (see :func:`_mirror_dir`);
    ``allow_shrink=True`` overrides after a deliberate truth re-emit.

    Before the mirror touches anything, the destination's current state is
    preserved as a hardlink generation under ``<dest>/.generations/``
    (:func:`_snapshot_generation`) — the recovery margin for destruction the
    in-run guards can't see. ``archive restore-drill`` proves the mirror (or a
    generation) actually restores.
    """
    from .._api import open_archive

    open_archive(home)
    from .._truth import checkpoint as _checkpoint
    from .._truth.jsonl_log import _truth_write_lock, _try_rebalance_lock

    _checkpoint()  # full: overlays + metadata-update backstop → truth is a complete restore set
    # Verify AFTER the checkpoint, so verify_ok describes the tree the mirror
    # actually copies — a verdict on the pre-checkpoint state could bless (or
    # smear) a different truth than the one being backed up.
    verify_ok = True
    if verify_first:
        verify_ok = bool(verify(home=home)["ok"])

    paths = resolve_paths(home)
    # Refresh the durable vector cache so live-embedded vectors (the watcher's
    # cohost writes them into index.db only) survive an index loss and ride the
    # mirror. No-op when the store holds no vectors — an empty save never
    # replaces a populated sidecar.
    from .._retrieval.vectors import save_vectors_sidecar

    vectors_cached = save_vectors_sidecar(paths.truth_dir)
    dest_path = Path(dest).expanduser()
    dest_path.mkdir(parents=True, exist_ok=True)
    # A destination on the same filesystem as the truth dir protects against a
    # bad write, not against the disk: one device failure (or a stolen machine)
    # takes source, mirror, and every hardlink generation together. Report-only
    # — a same-disk mirror still beats none — but the flag rides the result and
    # the health record so `archive status` and anything watching health.json
    # can keep saying so until a real second copy exists.
    try:
        same_device = os.stat(dest_path).st_dev == os.stat(paths.truth_dir).st_dev
    except OSError:
        same_device = None
    # Delete-sync only when the source looks like a real, checkpointed truth dir —
    # a mirror of an empty/foreign source must never strip a good backup — and only
    # when it verified clean (a sick source mirrors additively; see docstring).
    delete = (paths.truth_dir / "manifest.json").exists() and verify_ok
    # Preserve the destination's pre-run state as a hardlink generation BEFORE
    # the mirror overwrites it — the recovery margin for anything the guards
    # can't see (same-size corruption, a mistaken --allow-shrink, a bad repair).
    generations = _snapshot_generation(dest_path)
    with _try_rebalance_lock() as held:
        # If a rebalance sweep is mid-flight, mirror additively (no deletions):
        # copies of both layouts are safe; stale-path deletion waits for the next run.
        #
        # The truth-write mutex is held for the whole traversal so the mirror is a
        # drain-consistent snapshot: no append batch can land in (or be rolled back
        # out of) a truth file between the mirror reading one file and the next.
        # Without it the copy can capture a mid-drain partial batch — one thread's
        # file with a transaction's rows and another's without — or a pre-rollback
        # append that the drain then truncates away, which the shrink guard would
        # afterwards pin in the mirror as a divergent file. Writers pause for the
        # traversal; incremental runs copy little, so the pause is brief.
        with _truth_write_lock():
            result = _mirror_dir(
                paths.truth_dir, dest_path, delete=delete and held, allow_shrink=allow_shrink
            )
    result.update(generations)

    # Structural completeness: every source .jsonl must exist at the destination,
    # at ≥ its size at copy time (append-only files may have grown since).
    missing = divergent = 0
    for sp in paths.truth_dir.rglob("*.jsonl"):
        dp = dest_path / sp.relative_to(paths.truth_dir)
        try:
            ds = dp.stat()
        except OSError:
            missing += 1
            continue
        if ds.st_size > sp.stat().st_size:
            divergent += 1  # dest larger than source: divergent copy, not growth
    result["mirror_complete"] = missing == 0 and divergent == 0
    result["dest_missing_files"] = missing
    result["dest_divergent_files"] = divergent

    # Record the outcome in the home's health file (surfaced by `archive status`):
    # a backup agent that quietly stops running is indistinguishable from a
    # healthy one by its log files alone — the record's age is the signal.
    record_health("backup_last", {
        "dest": str(dest_path),
        "ok": bool(verify_ok and result["mirror_complete"] and not result["deletions_skipped"]),
        "verify_ok": verify_ok,
        "mirror_complete": result["mirror_complete"],
        "files_copied": result["files_copied"],
        "same_device": same_device,
    })
    stamp_heartbeat()
    return {
        "truth_dir": str(paths.truth_dir),
        "dest": str(dest_path),
        "vectors_cached": vectors_cached,
        "verify_ok": verify_ok,
        "same_device": same_device,
        **result,
    }


def restore_drill(
    dest: str, *, home: Optional[str] = None, keep_home: bool = False
) -> dict:
    """Prove the backup actually restores: rebuild a full index from the mirror
    in a throwaway home and check what materialized against the mirror's own scan.

    ``verify --backup`` parses and counts the mirror; this is the missing last
    step — an end-to-end rehearsal of the recovery path (copy the truth dir,
    ``reindex`` from it: schema, per-thread load, kg replay, FTS, the vector
    sidecar restore, quick_check). ``ok`` means the mirror parsed clean, the
    rebuilt index materialized exactly the mirror's effective counts, the
    restored archive covers the live one (``coverage`` = rebuilt events / live
    events ≥ 0.98 — the mirror is minutes old when the scheduled drill runs, so
    materially lower means the backup restores to less than the archive it is
    supposed to protect), and a smoke pass (:func:`_drill_smoke`) proved the
    rebuilt archive actually *reads and searches*, not just materializes.
    Heavy (a full index build) — sized for the nightly 04:00 window, where
    ``archive nightly`` runs it after every backup.

    The drill home is a temp directory (``keep_home=True`` keeps it for
    inspection, e.g. to point a reader at the restored index); the live archive
    is reopened before returning, and the outcome lands in ``health.json``
    (``restore_drill_last``) so a drill that stops running looks stale."""
    import shutil
    import tempfile
    import time

    from .._api import close, open_archive

    paths = open_archive(home)
    from .._truth import scan_truth_counts

    dest_path = Path(dest).expanduser()
    if not (dest_path / "threads").exists():
        return {"dest": str(dest_path), "ok": False, "error": "not a truth mirror"}
    live_home = str(paths.home)
    started = time.monotonic()
    from sqlalchemy import func, select

    from .._store import Event, get_session

    with get_session() as s:
        live_events = s.execute(select(func.count()).select_from(Event)).scalar() or 0
    scan = scan_truth_counts(truth_dir=dest_path)
    result: dict = {"dest": str(dest_path), "mirror": scan, "live_events": int(live_events)}
    drill_home = Path(tempfile.mkdtemp(prefix="thread-archive-restore-drill-"))
    try:
        # The generations subtree and any half-published mirror temp files are
        # destination bookkeeping, not truth — the drill restores the mirror.
        # copyfile, not the default copy2: the drill consumes JSONL content
        # only, and metadata replication EPERMs on network mirrors (smbfs
        # refuses reads of system xattrs like com.apple.provenance).
        shutil.copytree(
            dest_path, drill_home / "truth",
            copy_function=shutil.copyfile,
            ignore=shutil.ignore_patterns(_GENERATIONS_SUBDIR, ".*.tmp-*"),
        )
        open_archive(str(drill_home))
        from .._truth import reindex as _reindex

        try:
            counts = _reindex()
        except RuntimeError as e:  # a refused/failed rebuild IS the drill's finding
            result.update({"ok": False, "error": str(e)})
            counts = None
        if counts is not None:
            result["rebuilt"] = counts
            result["coverage"] = round(counts["events"] / (live_events or 1), 6)
            result["smoke"] = _drill_smoke(
                str(drill_home), expect_content=counts["events"] > 0
            )
            result["ok"] = (
                scan["parse_errors"] == 0
                and counts["events"] == scan["events_effective"]
                and counts["threads"] == scan["threads"]
                and result["coverage"] >= 0.98
                and result["smoke"]["ok"]
            )
    finally:
        close()
        if keep_home:
            result["drill_home"] = str(drill_home)
        else:
            shutil.rmtree(drill_home, ignore_errors=True)
        open_archive(live_home)
    result["seconds"] = round(time.monotonic() - started, 1)
    record_health("restore_drill_last", {
        "dest": str(dest_path),
        "ok": bool(result.get("ok")),
        "events": scan["events_effective"],
        "coverage": result.get("coverage"),
        "seconds": result["seconds"],
    })
    stamp_heartbeat()
    return result


def list_generations(dest: str) -> list[str]:
    """The mirror's retained pre-run snapshots, newest first (see
    :func:`_snapshot_generation`). Each name is a UTC stamp restorable via
    ``restore(..., generation=name)``; an empty list means the mirror has no
    generations yet (fewer than two backup runs, or a dest that can't snapshot)."""
    gens = Path(dest).expanduser() / _GENERATIONS_SUBDIR
    if not gens.is_dir():
        return []
    return sorted(
        (p.name for p in gens.iterdir() if p.is_dir() and not p.name.startswith(".")),
        reverse=True,
    )


def restore(
    dest: str,
    to: str,
    *,
    generation: Optional[str] = None,
    replace: bool = False,
    allow_parse_errors: bool = False,
) -> dict:
    """Restore a *real* archive home from a backup mirror — the productized
    recovery path (:func:`restore_drill` proves the mirror restores; this one
    actually restores it).

    The sequence is preflight → staged rebuild → verify → atomic publish:

    - **Preflight.** The source (the mirror head, or ``<mirror>/.generations/<generation>``
      when a generation is named) must be a truth mirror; its scan must parse
      clean (``allow_parse_errors=True`` restores a dirty mirror anyway — in a
      real disaster a flawed copy beats none, but that is an explicit choice).
      A non-empty target home is refused without ``replace=True``.
    - **Staged rebuild.** The mirror is copied into a staging home *next to* the
      target (same filesystem, so publication is a rename), and a full
      ``reindex`` builds the index there. Nothing at the target is touched yet.
    - **Verify.** The rebuilt counts must equal the mirror scan's effective
      counts, and the smoke pass (:func:`_drill_smoke`) must prove the staged
      archive reads and searches. A staging home that fails verification is
      deleted and reported — the target is never replaced with a dud.
    - **Publish.** With ``replace``, the existing home is moved aside to
      ``<home>.damaged-<stamp>`` — preserved, never deleted — then the staging
      home renames into place. A post-publish reopen re-counts events as a
      final sanity check.

    Returns a report dict (``ok``, mirror scan, rebuilt counts, smoke, the
    damaged-home path when one was set aside, seconds) and records
    ``restore_last`` in the restored home's health.json. Leaves the process's
    archive pointed at the restored home."""
    import shutil
    import time
    from datetime import datetime, timezone

    from .._api import close, open_archive
    from .._truth import scan_truth_counts

    started = time.monotonic()
    dest_path = Path(dest).expanduser()
    source = dest_path / _GENERATIONS_SUBDIR / generation if generation else dest_path
    result: dict = {"dest": str(dest_path), "generation": generation, "to": to, "ok": False}
    if not (source / "threads").exists():
        result["error"] = f"not a truth mirror: {source}"
        return result

    scan = scan_truth_counts(truth_dir=source)
    result["mirror"] = scan
    if scan["parse_errors"] and not allow_parse_errors:
        result["error"] = (
            f"mirror has {scan['parse_errors']} parse error(s) — restore refused "
            f"(allow_parse_errors=True restores it anyway)"
        )
        return result

    to_path = Path(to).expanduser()
    if to_path.exists() and any(to_path.iterdir()) and not replace:
        result["error"] = f"target home {to_path} is not empty — pass replace=True to set it aside"
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    to_path.parent.mkdir(parents=True, exist_ok=True)
    staging = to_path.parent / f".{to_path.name}.restoring-{stamp}"
    try:
        # copyfile (not copy2) for the same reason as the drill: metadata
        # replication EPERMs on network mirrors.
        shutil.copytree(
            source, staging / "truth",
            copy_function=shutil.copyfile,
            ignore=shutil.ignore_patterns(_GENERATIONS_SUBDIR, ".*.tmp-*"),
        )
        staging.chmod(0o700)
        open_archive(str(staging))
        from .._truth import reindex as _reindex

        try:
            counts = _reindex()
        except RuntimeError as e:
            result["error"] = f"reindex failed: {e}"
            counts = None
        if counts is not None:
            result["rebuilt"] = counts
            result["smoke"] = _drill_smoke(str(staging), expect_content=counts["events"] > 0)
            verified = (
                counts["events"] == scan["events_effective"]
                and counts["threads"] == scan["threads"]
                and result["smoke"]["ok"]
            )
            if not verified:
                result["error"] = (
                    "staged rebuild failed verification (counts or smoke) — "
                    "target left untouched"
                )
                counts = None
        close()
        if counts is None:
            shutil.rmtree(staging, ignore_errors=True)
            return result

        # Publish: set a damaged home aside (never delete), rename staging in.
        if to_path.exists():
            damaged = to_path.with_name(f"{to_path.name}.damaged-{stamp}")
            os.rename(to_path, damaged)
            result["damaged_home"] = str(damaged)
        os.rename(staging, to_path)
    except Exception as e:  # noqa: BLE001 — the report is the contract; never half-raise
        close()
        shutil.rmtree(staging, ignore_errors=True)
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    # Post-publish sanity: the renamed home opens and holds what staging held.
    open_archive(str(to_path))
    from sqlalchemy import func, select

    from .._store import Event, get_session

    with get_session() as s:
        published = s.execute(select(func.count()).select_from(Event)).scalar() or 0
    result["ok"] = published == scan["events_effective"]
    if not result["ok"]:
        result["error"] = (
            f"post-publish count mismatch: {published} events at {to_path}, "
            f"expected {scan['events_effective']}"
        )
    result["seconds"] = round(time.monotonic() - started, 1)
    record_health("restore_last", {
        "dest": str(dest_path),
        "generation": generation,
        "to": str(to_path),
        "ok": bool(result["ok"]),
        "events": scan["events_effective"],
        "seconds": result["seconds"],
    })
    return result


def _drill_smoke(home: str, *, expect_content: bool) -> dict:
    """Exercise the restored archive the way a reader would — the last gap
    between "the index materialized" and "the archive is usable." Reads the
    newest indexed thread and searches for a token drawn from the FTS shadow's
    own stored content (the exact corpus ``event_search`` matches against, so a
    hit is guaranteed when the search surface works). A rebuilt index whose
    read path or search surface is broken must fail the drill here, not on the
    first real restore. Runs against the drill home while it is still open."""
    import re as _re

    from sqlalchemy import text as _sa_text

    from .._api import read_thread, search
    from .._store import get_session as _get_session

    out: dict = {"ok": not expect_content, "read_ok": False, "search_ok": False}
    if not expect_content:
        return out
    try:
        with _get_session() as s:
            rows = s.execute(_sa_text(
                "SELECT thread_id, content FROM events_fts "
                "WHERE content != '' ORDER BY event_id DESC LIMIT 50"
            )).fetchall()
        tid = token = None
        for thread_id, content in rows:
            token = next(iter(_re.findall(r"[A-Za-z]{4,}", content or "")), None)
            if token:
                tid = thread_id
                break
        if tid is None or token is None:
            # No sampleable token (empty FTS surface would already fail the
            # count checks; all-non-Latin content just isn't sampleable here).
            out["ok"] = True
            out["skipped"] = "no sampleable token in the newest FTS rows"
            return out
        text = read_thread(tid, home=home, mode="chat", limit=20)
        out["read_ok"] = bool(text and text.strip())
        out["token"] = token
        out["search_ok"] = bool(search(token, home=home, limit=5, rerank=False))
        out["ok"] = out["read_ok"] and out["search_ok"]
    except Exception as e:  # a crash in the read/search path IS the finding
        out["error"] = f"{type(e).__name__}: {e}"
        out["ok"] = False
    return out
