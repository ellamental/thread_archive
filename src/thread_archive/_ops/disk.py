"""What the archive costs on disk, and how much of that is rebuildable.

An archive home grows several times larger than the conversations in it: the
SQLite projection routinely exceeds the JSONL it is built from, the vector pack
sits beside it, the raw source mirror and drift quarantine keep provider files
the provider itself has pruned, and migrations, repairs, and logs leave payloads
behind that nothing ever collects. None of that is visible from a thread count,
so "why is this 30 GB" has no answer anywhere in the product without this module.

The breakdown answers the only question a size prompts — *what of this can I get
rid of* — by sorting every top-level entry into four kinds:

``truth``
    The append-only JSONL log. Irreplaceable: it is the archive.
``index``
    ``index.db`` (with its WAL/SHM siblings) and ``vector-pack/``. Derived
    projections, rebuildable from truth with ``reindex`` + ``embed`` — cheap to
    delete, though re-embedding costs hours of model time rather than minutes.
``sources``
    ``source-mirror/`` and ``dumps/``: verbatim provider files and the drift
    quarantine. Deliberately never auto-deleted, because their whole purpose is
    outliving the harness's own retention — so they grow without bound and are
    the operator's call, not the archive's.
``other``
    Everything else — logs, telemetry ledgers, migration backups, bench caches.
    Individually small by design and collectively not, which is why
    :func:`disk_usage` names the largest of them rather than reporting one
    anonymous remainder.

One ``scandir`` walk of the home costs well under a second on a 40k-file, 30 GB
archive, so callers pay for it directly rather than caching a number that would
be wrong exactly when someone is watching it move. It is nonetheless kept out of
:func:`thread_archive._api.status`, whose result the viewer polls on a timer.

Symlinks are never followed and never sized. A home reached through the
compatibility symlink still measures its real contents; a symlink *into* the home
pointing at a backup elsewhere would otherwise report someone else's disk as this
archive's.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .._config import resolve_paths

# The kinds, in report order — least disposable first, so a reader meets the
# irreplaceable bytes before the ones they might reclaim.
KINDS = ("truth", "index", "sources", "other")


def _tree_bytes(path: Path) -> tuple[int, int]:
    """``(bytes, files)`` for a path: one stat for a file, a recursive
    ``scandir`` for a directory.

    Symlinks are counted as themselves and never traversed, and an entry that
    vanishes or refuses to be read mid-walk is skipped: this is advisory
    reporting over a directory a live watcher is writing to, so a race must
    round the number, never raise.
    """
    try:
        st = path.lstat()
    except OSError:
        return 0, 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_size, 1

    total = 0
    files = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _classify(entry: Path, paths) -> str:
    """Which of :data:`KINDS` an entry in the home belongs to.

    Matched against the *resolved* truth and index locations rather than their
    default names, so an archive whose truth or index was pointed elsewhere is
    still classified by what a path is, not by what it is called.
    """
    if entry == paths.truth_dir:
        return "truth"
    # The index's WAL and SHM siblings carry its name plus a suffix, and a
    # checkpointing WAL is briefly gigabytes — the same kind as what it fronts.
    if entry.name.startswith(paths.index_path.name) and entry.parent == paths.index_path.parent:
        return "index"
    if entry.name == "vector-pack":
        return "index"
    if entry.name in ("source-mirror", "dumps"):
        return "sources"
    return "other"


def disk_usage(*, home: Optional[str] = None, top: int = 12) -> dict:
    """Measure the archive home: a total, a per-kind split, and the largest
    entries by name.

    ``top`` bounds the named entries only. It is a display bound on an otherwise
    complete accounting — every byte under the home lands in exactly one kind's
    total whether or not its entry is named.

    Truth and index live under the home by default but need not (both have
    environment overrides). One that resolves outside is measured where it
    actually is and listed in ``external``, so the total is the archive's real
    footprint rather than the footprint of one directory.

    Read-only, and never creates the home it measures: an absent archive reports
    zeros rather than scaffolding one as a side effect of being asked its size.
    """
    paths = resolve_paths(home)
    kinds: dict[str, int] = dict.fromkeys(KINDS, 0)
    entries: list[dict] = []
    external: list[str] = []
    total = 0
    files = 0

    try:
        children = sorted(paths.home.iterdir())
    except OSError:
        children = []

    for child in children:
        size, count = _tree_bytes(child)
        kind = _classify(child, paths)
        kinds[kind] += size
        total += size
        files += count
        entries.append({"name": child.name, "bytes": size, "kind": kind})

    # A truth dir or index outside the home is part of this archive and part of
    # its cost; it is simply not under the directory just walked.
    for path, kind in ((paths.truth_dir, "truth"), (paths.index_path, "index")):
        try:
            if path.parent == paths.home or not path.exists():
                continue
        except OSError:
            continue
        size, count = _tree_bytes(path)
        kinds[kind] += size
        total += size
        files += count
        entries.append({"name": path.name, "bytes": size, "kind": kind})
        external.append(str(path))

    entries.sort(key=lambda e: e["bytes"], reverse=True)
    return {
        "home": str(paths.home),
        "total_bytes": total,
        "files": files,
        "kinds": kinds,
        # Rebuildable from truth, and the number a reclamation decision turns on.
        "rebuildable_bytes": kinds["index"],
        "entries": entries[:top],
        "external": external,
    }


def format_bytes(n: Optional[int]) -> str:
    """Bytes as an operator reads them — a small number and a unit that keeps it
    small. Binary units, matching what ``du`` reports for the same directory."""
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if abs(value) < 1024.0:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"
