"""Append-only JSONL telemetry ledgers, with rotation that never destroys.

The home root carries several of these — retrieval usage, web requests, ingest
passes, load runs — and they are all the same object: a file of one-JSON-record
lines, appended by a process that must not fail if the write fails, capped so no
single file grows unbounded. This is that object, owned once.

**Rotation retains.** At :func:`append`'s cap the current file is renamed to a
UTC-stamped sibling (``web-requests.jsonl.20260728T144233Z``) and a fresh file
starts. Nothing is ever deleted or overwritten, so a ledger's history is the whole
history: every percentile, every before/after, every regression hunt reads the
full record rather than whatever survived the last rotation. A cap that clobbered
its predecessor would silently bound the analysis window to the busiest recent
stretch — precisely the stretch a slow-regression question needs to see past.

That makes total ledger size a growing number and it is meant to. These files are
small against the archive they describe (the index is measured in GB; a year of
request rows is measured in MB), and the operator-visible cost of keeping them is
far below the cost of discovering a regression that started in a segment nobody
kept. Pruning, if it is ever wanted, is a deliberate act on segments that exist —
not a side effect of writing the next row.

The cap is per *segment*, not per ledger: it bounds what one read has to walk and
what one torn write can cost, nothing else.

:func:`iter_rows` is the reader half and the reason segmenting is invisible to
callers — it walks every segment oldest-first (or newest-first for the readers
that want recency), so an analysis written against a ledger keeps working when the
file underneath it becomes six files.

Every write is fail-soft: telemetry must never break the work it describes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: A rotated segment: the ledger name plus a UTC stamp. Matched strictly rather
#: than by a bare ``.*`` glob, so unrelated siblings a human left beside a ledger
#: (``.superseded``, ``.bak``, an editor's swap file) are never read back as
#: telemetry.
_STAMPED = re.compile(r"^\d{8}T\d{6}Z(?:\.\d+)?$")

#: A pre-stamp rotation. Read, never written — it is one file and it is older
#: than any stamped segment beside it.
_LEGACY = re.compile(r"^\d+$")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sort_key(suffix: str) -> tuple[int, Any, int]:
    """Order segments oldest-first. Numeric rotations precede every stamped one:
    only the pre-stamp scheme wrote them, so they predate every stamp whatever
    their number says.

    The same-second disambiguator is compared as a *number*, not as text — sorted
    lexically, a tenth rotation inside one second would file itself between the
    first and the second."""
    if _LEGACY.match(suffix):
        return (0, "", int(suffix))
    stamp, _, counter = suffix.partition(".")
    return (1, stamp, int(counter) if counter else 0)


def segments(path: Path) -> list[Path]:
    """Every segment of the ledger at ``path``, oldest first, current file last.

    The current file is included when it exists and is always last — it is the one
    still being appended to. A ledger that has never been written returns ``[]``,
    which readers treat as an empty history rather than an error: a young archive
    has nothing to say, and that is not a failure to say it."""
    path = Path(path)
    out: list[tuple[tuple[int, Any, int], Path]] = []
    try:
        for sibling in path.parent.iterdir():
            if not sibling.name.startswith(path.name + "."):
                continue
            suffix = sibling.name[len(path.name) + 1:]
            if _STAMPED.match(suffix) or _LEGACY.match(suffix):
                out.append((_sort_key(suffix), sibling))
    except OSError:
        return [path] if path.exists() else []
    ordered = [p for _, p in sorted(out, key=lambda kv: kv[0])]
    if path.exists():
        ordered.append(path)
    return ordered


def rotate(path: Path) -> Optional[Path]:
    """Move ``path`` aside to a stamped segment and return where it landed.

    ``None`` when there was nothing to rotate. The stamp is per-second, so a
    second rotation inside one second takes a ``.1``-style disambiguator rather
    than replacing the first — the one case where a naive stamp would still lose a
    file."""
    path = Path(path)
    if not path.exists():
        return None
    base = f"{path.name}.{_stamp()}"
    dest = path.with_name(base)
    n = 0
    while dest.exists():  # same-second rotation: never land on an existing segment
        n += 1
        dest = path.with_name(f"{base}.{n}")
    path.replace(dest)
    return dest


def append(path: Path, record: dict, *, max_bytes: int) -> None:
    """Append one record to the ledger at ``path``, rotating first at ``max_bytes``.

    Never raises: a telemetry write that fails is logged and dropped, because the
    alternative is a metrics bug taking down ingest or a search."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= max_bytes:
                rotate(path)
        except FileNotFoundError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    except OSError:
        logger.warning("could not append to ledger %s", path, exc_info=True)


def iter_rows(path: Path, *, newest_first: bool = False) -> Iterator[dict]:
    """Every record in the ledger at ``path``, across all its segments.

    Oldest first by default — the order the rows were written, which is what a
    timeseries wants. ``newest_first`` reverses both the segment order and the
    lines within each, for the readers that want the recent distribution and stop
    early.

    A line that doesn't parse is skipped rather than raising: the last line of a
    live ledger can be torn by a concurrent append, and one torn line is not a
    reason to refuse the other million."""
    for seg in reversed(segments(path)) if newest_first else segments(path):
        try:
            with open(seg, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines) if newest_first else lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def total_bytes(path: Path) -> int:
    """Bytes across every segment — what retaining this ledger currently costs."""
    total = 0
    for seg in segments(path):
        try:
            total += seg.stat().st_size
        except OSError:
            continue
    return total


def env_max_bytes(var: str, default: int) -> int:
    """A ledger's segment cap from ``var``, falling back to ``default``.

    Read per call rather than resolved at import: a constant would answer once at
    module load and ignore any later word on it, and these processes are
    long-lived."""
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
