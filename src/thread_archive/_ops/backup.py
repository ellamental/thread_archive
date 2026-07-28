"""Backup: mirror the JSONL truth to a destination, and prove it restores.

A ``cp``/``rsync`` of the truth dir *is* the backup (index.db is rebuildable from
it), so :func:`backup` checkpoints, verifies, then mirrors incrementally — with
the guards that keep a sick source or a mid-flight rebalance from destroying the
last good copy (shrink guard, bounded delete-sync, hardlink generations).
:func:`restore_drill` is the end-to-end rehearsal: rebuild a full index from the
mirror in a throwaway home and prove it reads and searches.

The destination is a *complete* restore set, not just the truth: each
run also syncs the home's non-truth recovery material into a reserved
``<dest>/.recovery/`` subtree (:func:`_sync_recovery_bundle`) — ``config.json``,
the retained
original exports under ``dumps/imported/``, and a reference snapshot of
``health.json`` + the operational ledgers. :func:`restore` installs the choices
and recovery material into the recovered home; the history snapshots stay at the
mirror.

Scope — what this backup is *for*: recovering from bad writes (corruption, a
botched migration, an errant delete). Durability of the disk itself is not
this product's concern: the archive is ordinary files, no different from any
other data on the machine, and it is covered by whatever the user runs for
the rest of their data. The product takes no position on where the
destination lives, does not inspect the host's backup arrangements, and
neither should anyone reviewing or operating it.
"""

from __future__ import annotations

import filecmp
import os
from pathlib import Path
from typing import Optional

from .._config import ArchivePaths, resolve_paths
from .health import record_health, stamp_heartbeat
from .verify import verify

# Delete-sync sanity bound: refuse to delete more than this fraction of the
# destination's files (once past the absolute floor). A mass-wipe of the source —
# a bug emptying truth/, an accidental rm — must not propagate to the last backup;
# a legitimate mass-move (shard rebalance) re-homes files, so the copies land
# first and the stale paths deleted stay a bounded fraction only when the copy
# half of the mirror actually ran.
MIRROR_DELETE_FLOOR = 64
MIRROR_DELETE_MAX_FRACTION = 0.25

# Destination-side generation snapshots: hardlink copies of the mirror's state,
# taken before each backup run overwrites it. See _snapshot_generation.
_GENERATIONS_SUBDIR = ".generations"
_GEN_KEEP_RECENT = 7
_GEN_KEEP_MONTHS = 6

# The recovery bundle: the home's non-truth recovery material, synced beside the
# truth mirror. Head-only by design — never snapshotted into generations — so it
# tracks the install's *current* state; a member deleted at the home leaves the
# backup on the next run instead of persisting in dated snapshots. The
# generations exist to recover the truth from bad writes, which this material is
# not. See _sync_recovery_bundle.
_RECOVERY_SUBDIR = ".recovery"
# Home-level files that ride the bundle. health.json and the
# ledgers are reference snapshots — bundled so operational history survives disk
# loss, but never installed by restore (a restored home must not claim the
# source install's health history).
_BUNDLE_HOME_FILES = (
    "config.json",
    "health.json",
    "capture-skips.jsonl",
    "seen-versions.json",
    "validation-drift.jsonl",
    "verify-failures.jsonl",
)


def _is_append_only_truth(rel: Path) -> bool:
    """True for truth files that only ever grow in normal operation: the per-thread
    files and the topic-graph event log. The cross-thread overlay snapshots and
    ``import_state.jsonl`` are full rewrites and may legitimately shrink."""
    return (
        rel.parts[:1] == ("threads",) or rel.name == "kg_events.jsonl"
    ) and rel.suffix == ".jsonl"


def _chmod_private(path: Path, mode: int) -> None:
    """Best-effort ``chmod``: backup destinations must never be more readable
    than the live home (0700 dirs / 0600 files), but network filesystems (smbfs)
    answer EPERM to chmod — there, access is the share's ACL problem, and a
    failed tighten must not fail the backup that privacy exists to protect."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _mkdir_private(d: Path) -> None:
    """``mkdir -p`` with every newly created component tightened to 0700.
    ``mkdir(mode=...)`` is umask-masked and applies only to the leaf, so each
    missing ancestor is created and chmodded explicitly. Pre-existing dirs are
    left alone — the backup entrypoint tightens the destination root itself."""
    missing = []
    cur = d
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    for nd in reversed(missing):
        try:
            nd.mkdir()
        except FileExistsError:
            continue
        _chmod_private(nd, 0o700)


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
        # Owner-only regardless of what copy2 replicated: these are private
        # conversation payloads, and the mode must not depend on source-mode
        # history or drift.
        _chmod_private(tmp, 0o600)
        os.replace(tmp, dp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mirror_dir(
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

    **Provably superseded twins are exempt from that bound.** A shard rebalance
    moves every thread file at once, and the one-shot ULID migration renames
    every thread id at once — either way the stale old-generation copies at the
    destination can vastly exceed the cap, and skipping them forever leaves the
    backup double-sized and holding records a restore must not see (a rebuild
    loads each thread twice and aborts). A doomed ``threads/**`` file whose
    supersession is provable at *both* ends is deleted regardless of the cap
    (see :func:`_split_superseded_twins`): *re-homed* — its thread has a file at
    the source's canonical shard depth and the destination's canonical copy is
    at least as large as the stale one (``rehomed_twins_deleted``) — or
    *renamed* — the ULID migration's durable mapping names its successor, whose
    file exists at the source with a non-empty destination copy
    (``renamed_twins_deleted``). Everything else stays capped.

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
            # Skip unchanged files. The mtime check (truncated to whole seconds so
            # a coarse-resolution mirror filesystem doesn't force a recopy every
            # run) is the fast path for the append-only bulk. But the checkpoint
            # rewrites the full-rewrite overlays (manifest.json, import_state.jsonl,
            # the knowledge-layer files) on every backup — identical content, fresh
            # mtime — so a same-size file whose mtime reads newer is byte-compared
            # before recopying: an identical rewrite must not churn the mirror (and,
            # across a one-second boundary, spuriously report a non-incremental run).
            if ss.st_size == ds.st_size and (
                int(ss.st_mtime) <= int(ds.st_mtime)
                or filecmp.cmp(sp, dp, shallow=False)
            ):
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
        _mkdir_private(dp.parent)
        _atomic_copy(sp, dp, trim_to_newline=_is_append_only_truth(rel))
        copied += 1
        total += dp.stat().st_size
        synced_dirs.add(dp.parent)
    for sd in synced_dirs:
        _fsync_dir(sd)  # the renames that published this pass's copies must stick
    twins_deleted = renamed_deleted = 0
    if delete:
        # The generations subtree (hardlink snapshots of prior mirror runs) and
        # the recovery bundle (synced by its own pass, with its own deletion
        # rules) are destination-side state, not truth copies — never deletion
        # candidates, and never counted toward the deletion cap's denominator.
        dest_files = [
            dp for dp in dest.rglob("*")
            if not dp.is_dir()
            and dp.relative_to(dest).parts[0] not in (_GENERATIONS_SUBDIR, _RECOVERY_SUBDIR)
        ]
        doomed = [dp for dp in dest_files if not (src / dp.relative_to(dest)).exists()]
        if doomed:
            rehomed, renamed, doomed = _split_superseded_twins(src, dest, doomed)
            for dp in (*rehomed, *renamed):
                dp.unlink()
                deleted += 1
            twins_deleted = len(rehomed)
            renamed_deleted = len(renamed)
        limit = max(MIRROR_DELETE_FLOOR, int(len(dest_files) * MIRROR_DELETE_MAX_FRACTION))
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
        "renamed_twins_deleted": renamed_deleted,
        "deletions_skipped": skipped,
        "shrinks_skipped": shrinks,
        "shrink_sample": shrink_sample,
    }


def _ulid_mapping(src: Path) -> dict[str, str]:
    """The ULID migration's durable legacy-id → ULID record, read from
    ``ULID_MAPPING_FILE`` beside the truth dir. Empty when the home never
    migrated or the file is unreadable — renamed-twin detection simply stays
    off and those deletions remain capped."""
    import json

    from .._truth.layout import ULID_MAPPING_FILE

    try:
        raw = json.loads((src.parent / ULID_MAPPING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _split_superseded_twins(
    src: Path, dest: Path, doomed: list[Path]
) -> tuple[list[Path], list[Path], list[Path]]:
    """Partition planned mirror deletions into provably-superseded twins —
    re-homed or renamed — and everything else, which stays under the deletion
    cap (see :func:`mirror_dir`).

    A *re-homed* twin (shard rebalance) qualifies only when its thread's
    canonical-depth file exists at the source *and* the destination's copy of
    that canonical file is at least as large as the stale one — the rebalance
    sweep moves/merges whole files, so a genuine re-home can never leave the
    canonical copy smaller.

    A *renamed* twin (ULID migration) is a legacy-integer-named file whose id
    the migration's durable mapping (:func:`_ulid_mapping`) maps to a ULID
    whose canonical file exists at the source *and* has a non-empty copy at the
    destination. Sizes are not comparable across the migration's rewrite (every
    line changed), so the proof is the mapping record plus the successor's
    presence at both ends."""
    from .._truth.jsonl_log import THREADS_SUBDIR, _shard_depth, _thread_relpath

    depth = _shard_depth(src)
    mapping: Optional[dict[str, str]] = None  # loaded on the first legacy-named candidate
    rehomed: list[Path] = []
    renamed: list[Path] = []
    rest: list[Path] = []
    for dp in doomed:
        rel = dp.relative_to(dest)
        if rel.parts[0] == THREADS_SUBDIR and dp.suffix == ".jsonl":
            tid = dp.stem
            if tid:
                canonical = _thread_relpath(tid, depth)
                try:
                    if (
                        canonical != rel
                        and (src / canonical).exists()
                        and (dest / canonical).stat().st_size >= dp.stat().st_size
                    ):
                        rehomed.append(dp)
                        continue
                except OSError:  # canonical dest copy missing/unreadable — stay capped
                    pass
            if tid.isdigit():
                if mapping is None:
                    mapping = _ulid_mapping(src)
                successor = mapping.get(tid)
                if successor:
                    srel = _thread_relpath(successor, depth)
                    try:
                        if (src / srel).exists() and (dest / srel).stat().st_size > 0:
                            renamed.append(dp)
                            continue
                    except OSError:  # successor dest copy missing/unreadable — stay capped
                        pass
        rest.append(dp)
    return rehomed, renamed, rest


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
            _chmod_private(gens, 0o700)
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
            # The recovery bundle is head-only: a generation that retained a
            # copy would keep superseded install state alive in dated snapshots
            # for the whole retention window.
            if rel.parts[0] in (_GENERATIONS_SUBDIR, _RECOVERY_SUBDIR):
                continue
            gp = tmp / rel
            _mkdir_private(gp.parent)
            try:
                os.link(sp, gp)  # linked inode carries the mirror file's 0600
            except OSError:  # filesystem without hardlinks — take the copy cost.
                # copyfile, not copy2: a generation is a restore source, so only
                # content matters (retention is keyed by the generation dir's
                # name), and metadata replication EPERMs on network mirrors
                # (smbfs refuses reads of system xattrs like com.apple.provenance).
                shutil.copyfile(sp, gp)
                _chmod_private(gp, 0o600)
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


def _bundle_sources(paths: ArchivePaths) -> dict[Path, Path]:
    """The recovery bundle's file map: bundle-relative path → source path, for
    every bundle member that exists at the home. Membership is explicit — the
    home also holds the index, locks, and logs, none of which belong in a
    restore set (the index rebuilds from truth; locks and logs are process
    state)."""
    from .._watcher.export_drop import IMPORTED_DIRNAME

    out: dict[Path, Path] = {}
    for name in _BUNDLE_HOME_FILES:
        sp = paths.home / name
        if sp.is_file():
            out[Path(name)] = sp
    imported = paths.dumps_dir / IMPORTED_DIRNAME
    if imported.is_dir():
        for sp in imported.rglob("*"):
            if sp.is_file():
                rel = Path("dumps") / IMPORTED_DIRNAME / sp.relative_to(imported)
                out[rel] = sp
    return out


def _sync_recovery_bundle(paths: ArchivePaths, dest: Path, *, delete: bool) -> dict:
    """Sync the home's non-truth recovery material into ``<dest>/.recovery/`` —
    the difference between "the conversations survive" and "the install
    survives": operator choices (``config.json``), the
    retained original exports (``dumps/imported/``, the lossless re-import
    source), and reference snapshots of ``health.json`` + the operational
    ledgers.

    A true head-only mirror of the file map: changed files are copied
    atomically, and (``delete``, same gate as the truth mirror's delete-sync)
    bundle files with no live counterpart are removed — that is the path by
    which a superseded retained export or a deleted config actually leaves the
    backup. Never snapshotted into generations (see
    :func:`_snapshot_generation`)."""
    rec_dir = dest / _RECOVERY_SUBDIR
    sources = _bundle_sources(paths)
    copied = deleted = 0
    for rel, sp in sources.items():
        dp = rec_dir / rel
        if dp.exists():
            ss, ds = sp.stat(), dp.stat()
            if ss.st_size == ds.st_size and (
                int(ss.st_mtime) <= int(ds.st_mtime)
                or filecmp.cmp(sp, dp, shallow=False)
            ):
                continue
        _mkdir_private(dp.parent)
        _atomic_copy(sp, dp, trim_to_newline=False)
        copied += 1
    if delete and rec_dir.is_dir():
        keep = {rec_dir / rel for rel in sources}
        for dp in list(rec_dir.rglob("*")):
            if not dp.is_dir() and dp not in keep:
                dp.unlink()
                deleted += 1
        for dp in sorted((p for p in rec_dir.rglob("*") if p.is_dir()), reverse=True):
            try:
                dp.rmdir()  # only succeeds when empty
            except OSError:
                pass
    return {
        "bundle_files": len(sources),
        "bundle_copied": copied,
        "bundle_deleted": deleted,
    }


def _bundle_status(dest: Path) -> dict:
    """What the mirror's recovery bundle holds — presence and rough shape, so
    the restore drill can report whether a restore would get the install
    back, not just the conversations."""
    rec = dest / _RECOVERY_SUBDIR
    out: dict = {
        "present": rec.is_dir(),
        "config": (rec / "config.json").is_file(),
        "health": (rec / "health.json").is_file(),
        "retained_exports": 0,
    }
    imported = rec / "dumps" / "imported"
    if imported.is_dir():
        out["retained_exports"] = sum(1 for p in imported.rglob("*") if p.is_file())
    return out


def _restore_bundle(dest: Path, home: Path) -> dict:
    """Install the recovery bundle into a restored home: ``config.json`` and
    the retained exports — the operator's choices and the
    recovery material. The bundled health snapshot and ledgers deliberately
    stay at the mirror: they are the *source install's* operational history,
    reference material for forensics, not state the restored home may claim
    as its own. Fail-soft — a bundle that won't copy degrades to the pre-bundle
    restore (truth only), reported, never a failed restore."""
    rec = dest / _RECOVERY_SUBDIR
    out: dict = {"config": False, "retained_exports": 0}
    if not rec.is_dir():
        return out
    try:
        sp = rec / "config.json"
        if sp.is_file():
            _atomic_copy(sp, home / "config.json", trim_to_newline=False)
            out["config"] = True
        imported = rec / "dumps" / "imported"
        if imported.is_dir():
            for sp in imported.rglob("*"):
                if sp.is_file():
                    dp = home / "dumps" / "imported" / sp.relative_to(imported)
                    _mkdir_private(dp.parent)
                    _atomic_copy(sp, dp, trim_to_newline=False)
                    out["retained_exports"] += 1
    except OSError as e:
        out["error"] = str(e)
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
    ``truth/`` into ``dest`` incrementally.

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
    truth files are additionally shrink-guarded (see :func:`mirror_dir`);
    ``allow_shrink=True`` overrides after a deliberate truth re-emit.

    Before the mirror touches anything, the destination's current state is
    preserved as a hardlink generation under ``<dest>/.generations/``
    (:func:`_snapshot_generation`) — the recovery margin for destruction the
    in-run guards can't see. ``thread-archive backup drill`` proves the mirror (or a
    generation) actually restores.

    After the mirror, the run syncs the recovery bundle
    (:func:`_sync_recovery_bundle`): config, retained
    exports, and health/ledger snapshots land under ``<dest>/.recovery/``, so
    the destination restores the *install*, not just the conversations. Bundle
    failure is reported (``bundle_error``, and it fails the health record's
    ``ok``) but never aborts the truth mirror itself.
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
    _mkdir_private(dest_path)
    # Tighten a pre-existing destination too: the mirror must never sit more
    # readable than the 0700 live home it copies.
    _chmod_private(dest_path, 0o700)
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
            result = mirror_dir(
                paths.truth_dir, dest_path, delete=delete and held, allow_shrink=allow_shrink
            )
    result.update(generations)

    # The recovery bundle, outside the truth locks: its members have their own
    # atomic writers, so a copy races to the old or the new file, never a torn
    # one. Same delete gate as the truth mirror — a sick source syncs additively.
    try:
        result.update(_sync_recovery_bundle(paths, dest_path, delete=delete))
    except OSError as e:  # the bundle is protection for the install, not the truth mirror
        result["bundle_error"] = str(e)
        import logging

        logging.getLogger(__name__).exception("backup: recovery bundle sync failed")

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

    # Record the outcome in the home's health file (surfaced by `thread-archive status`):
    # a backup agent that quietly stops running is indistinguishable from a
    # healthy one by its log files alone — the record's age is the signal.
    record_health("backup_last", {
        "dest": str(dest_path),
        "ok": bool(
            verify_ok
            and result["mirror_complete"]
            and not result["deletions_skipped"]
            and "bundle_error" not in result
        ),
        "verify_ok": verify_ok,
        "mirror_complete": result["mirror_complete"],
        "files_copied": result["files_copied"],
    })
    stamp_heartbeat()
    return {
        "truth_dir": str(paths.truth_dir),
        "dest": str(dest_path),
        "vectors_cached": vectors_cached,
        "verify_ok": verify_ok,
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
    ``thread-archive backup nightly`` runs it after every backup.

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
    result: dict = {
        "dest": str(dest_path),
        "mirror": scan,
        "live_events": int(live_events),
        # What a restore would get back besides the conversations —
        # presence-only (the drill proves the truth restores; the bundle's
        # members are plain copies with their own atomic writers).
        "bundle": _bundle_status(dest_path),
    }
    drill_home = Path(tempfile.mkdtemp(prefix="thread-archive-restore-drill-"))
    try:
        # The generations subtree, the recovery bundle, and any half-published
        # mirror temp files are destination bookkeeping, not truth — the drill
        # restores the mirror. copyfile, not the default copy2: the drill
        # consumes JSONL content only, and metadata replication EPERMs on
        # network mirrors (smbfs refuses reads of system xattrs like
        # com.apple.provenance).
        shutil.copytree(
            dest_path, drill_home / "truth",
            copy_function=shutil.copyfile,
            ignore=shutil.ignore_patterns(_GENERATIONS_SUBDIR, _RECOVERY_SUBDIR, ".*.tmp-*"),
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
      clean (``allow_parse_errors=True`` restores a dirty mirror anyway — a
      flawed copy beats none, but that is an explicit choice).
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
      home renames into place, and the mirror's recovery bundle is installed
      (:func:`_restore_bundle`: config, retained exports — always
      from the mirror head, generations carry no bundle). A post-publish
      reopen re-counts events as a final sanity check.

    Returns a report dict (``ok``, mirror scan, rebuilt counts, smoke, the
    bundle installed, the damaged-home path when one was set aside, seconds)
    and records ``restore_last`` in the restored home's health.json. Leaves
    the process's archive pointed at the restored home."""
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
            ignore=shutil.ignore_patterns(_GENERATIONS_SUBDIR, _RECOVERY_SUBDIR, ".*.tmp-*"),
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
        # Install the recovery bundle — always from the mirror HEAD, even when
        # restoring a generation: the bundle is head-only by design, so the
        # head's is the only and the current one.
        result["bundle"] = _restore_bundle(dest_path, to_path)
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
        out["search_ok"] = bool(search(token, home=home, limit=5))
        out["ok"] = out["read_ok"] and out["search_ok"]
    except Exception as e:  # a crash in the read/search path IS the finding
        out["error"] = f"{type(e).__name__}: {e}"
        out["ok"] = False
    return out
