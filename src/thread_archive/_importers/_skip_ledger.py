"""The capture-skip ledger: ``<home>/capture-skips.jsonl``.

When an import advances a ``(source, source_id)`` watermark past source lines
that produced no events — the "no importable content" path, or a fresh thread
discarded because nothing in it imported — those lines are consumed: incremental
import never revisits them, and once the provider prunes the source file they are
unrecoverable. Usually that consumption is correct (metadata-only sessions,
empty transcripts). Under provider format drift it is silent data loss with the
same signature.

This ledger cannot tell the two apart — no code at this layer can — so it makes
the consumption *auditable and reversible* instead: every watermark advance past
never-imported lines appends one record here. After a parser fix, the records
say exactly which files to re-import (rewind the watermark, re-run import) while
their sources still exist. The capture-coverage check summarizes recent volume.

A record is closed by the re-import it called for, never erased —
:func:`record_resolution`, exactly as in the sibling drift ledger
(:mod:`._validation_ledger`), and for the same reason: the verdict that asked for
a repair must stop asking once the repair is proven, while the trail of what was
consumed stays whole. The proof is a re-read that did not skip again.

Deliberately NOT ledgered: a zero-yield poll on an existing thread. Turns
straddle polls and many providers write non-event lines, so that case is
routine; drift on existing threads is the coverage check's job (per-source
store-activity vs newest-archived-event).

Append-only JSONL, advisory, fail-soft — a ledger write must never break the
import it describes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "capture-skips.jsonl"

#: Marks a closing record; an observation carries no ``kind``. See
#: :data:`.._validation_ledger.RESOLUTION_KIND` — the two ledgers use one shape.
RESOLUTION_KIND = "resolution"


def record_skip(
    source: str,
    source_id: str,
    *,
    lines_skipped: int,
    lines_total: int,
    reason: str,
) -> None:
    """Append one consumption record. ``reason`` names the consuming path
    (``no_importable_content``, ``empty_import_discarded``)."""
    try:
        path = resolve_paths().home / LEDGER_FILE
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "source_id": source_id,
            "lines_skipped": int(lines_skipped),
            "lines_total": int(lines_total),
            "reason": reason,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning(
            "could not record capture skip for %s:%s", source, source_id, exc_info=True
        )


# The routine reason: a brand-new session with nothing importable — the metadata-
# only / empty-transcript case the module docstring calls out. It fires constantly
# (harnesses open sessions that never get used), so counting it toward the coverage
# *warning* trains the operator to ignore skips. Still ledgered and still counted in
# ``recent`` for the audit trail — only held out of ``recent_substantive``, which is
# what the health warning fires on. ``empty_import_discarded`` (a thread built then
# discarded because its content parsed to nothing) is the drift-adjacent case and
# stays substantive. A record with no/unknown reason counts as substantive too.
_ROUTINE_SKIP_REASON = "no_importable_content"


def _is_substantive(rec: dict) -> bool:
    """Whether a consumption record is evidence rather than routine trail. A
    closing record is neither and never reaches here."""
    return rec.get("reason") != _ROUTINE_SKIP_REASON


def _epoch(value: object) -> Optional[float]:
    """An ISO timestamp as a UTC epoch, or ``None`` if it isn't one. Naive stamps
    are read as UTC — every writer here emits UTC."""
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _rows(path: Path) -> Iterator[tuple[Optional[dict], Optional[float]]]:
    """Every line of the ledger as ``(record, epoch)``; ``(None, None)`` for a line
    that doesn't parse, nothing at all for a ledger that doesn't exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    yield None, None
                    continue
                if not isinstance(rec, dict):
                    yield None, None
                    continue
                yield rec, _epoch(rec.get("at"))
    except OSError:
        return


def _is_resolution(rec: dict) -> bool:
    return rec.get("kind") == RESOLUTION_KIND


def _absorb_resolution(rec: dict, into: dict[tuple[str, str], float]) -> None:
    """Fold one closing record into ``{(source, source_id): latest through}``."""
    through = _epoch(rec.get("through"))
    if through is None:
        return
    source = str(rec.get("source") or "")
    ids = rec.get("source_ids")
    if not isinstance(ids, list):
        return
    for sid in ids:
        key = (source, str(sid))
        if through > into.get(key, float("-inf")):
            into[key] = through


def _open_records(path: Path, source: str, source_ids: set[str], through: float) -> int:
    """How many substantive records for ``source`` name one of ``source_ids``,
    predate ``through``, and are not already closed."""
    candidates: list[tuple[str, float]] = []
    resolved: dict[tuple[str, str], float] = {}
    for rec, at in _rows(path):
        if rec is None:
            continue
        if _is_resolution(rec):
            _absorb_resolution(rec, resolved)
            continue
        if at is None or at > through or not _is_substantive(rec):
            continue
        if str(rec.get("source") or "") != source:
            continue
        sid = str(rec.get("source_id") or "")
        if sid in source_ids:
            candidates.append((sid, at))
    return sum(
        1 for sid, at in candidates if at > resolved.get((source, sid), float("-inf"))
    )


def record_resolution(
    source: str, source_ids: Iterable[str], *, through: datetime, by: str
) -> int:
    """Close ``source``'s skip records for ``source_ids`` dated at or before
    ``through``. Returns how many records that closes; writes nothing when zero.

    ``through`` is stamped by the caller before the re-read that justifies it, so a
    file the fixed importer still consumes without importing records a fresh skip
    after the stamp and stays open. See
    :func:`.._validation_ledger.record_resolution` — same contract, same reasons.
    """
    ids = {str(s) for s in source_ids if str(s)}
    if not ids:
        return 0
    path = resolve_paths().home / LEDGER_FILE
    closing = _open_records(path, source, ids, through.timestamp())
    if not closing:
        return 0
    try:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": RESOLUTION_KIND,
            "source": source,
            "source_ids": sorted(ids),
            "through": through.isoformat(),
            "by": by,
            "closed": closing,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning("could not record skip resolution for %s", source, exc_info=True)
        return 0
    return closing


def substantive_since(source: str, since: datetime) -> set[str]:
    """``source_id``s with a substantive skip record newer than ``since`` — the
    files still being consumed without importing, as of a repair that started then.
    Routine empty-session skips are not evidence and never appear here."""
    cutoff = since.timestamp()
    out: set[str] = set()
    for rec, at in _rows(resolve_paths().home / LEDGER_FILE):
        if rec is None or at is None or at <= cutoff:
            continue
        if _is_resolution(rec) or not _is_substantive(rec):
            continue
        if str(rec.get("source") or "") != source:
            continue
        sid = str(rec.get("source_id") or "")
        if sid:
            out.add(sid)
    return out


def settled_empty_ids(source: str) -> set[str]:
    """``source_id``s consumed exactly once, on the routine empty-session path.

    The signature of a session opened and never used: the watcher saw it, the
    importer judged its lines contentless, and nothing was ever appended to make
    it worth a second look. A parser gone blind to a changed format leaves a
    different trace — the session keeps growing, so it is consumed again and
    again, each pass adding another record for the same id — and repetition is
    the only tell available at this layer, since both cases carry the same
    reason. So a second record disqualifies an id, as does any non-routine
    consumption (``empty_import_discarded`` is drift-adjacent by construction).

    The capture-coverage check reads this to tell store activity the archive has
    accounted for from store activity it is failing to ingest. Fail-soft toward
    red: an unreadable ledger settles nothing.

    Counts *consumptions* — closing records are not consumptions and are skipped,
    or a repair would disqualify every id it closed by inflating its count.
    """
    counts: dict[str, int] = {}
    routine: dict[str, bool] = {}
    for rec, _ in _rows(resolve_paths().home / LEDGER_FILE):
        if rec is None or _is_resolution(rec):
            continue
        if rec.get("source") != source:
            continue
        sid = str(rec.get("source_id") or "")
        if not sid:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        routine[sid] = routine.get(sid, True) and not _is_substantive(rec)
    return {sid for sid, n in counts.items() if n == 1 and routine[sid]}


def summarize_skips(*, days: float = 7.0) -> dict:
    """Ledger volume: total records ever, and records + lines within ``days``.
    ``recent_substantive`` is the subset of recent records whose reason is not the
    routine empty-session case (see ``_ROUTINE_SKIP_REASON``) and which no repair
    has closed (see :func:`record_resolution`) — the count the coverage warning
    fires on, so neither a steady trickle of empty sessions nor an already-repaired
    consumption cries wolf. ``recent_resolved`` counts the closed ones.
    ``by_source`` breaks the recent window down per source —
    ``{source: {recent, recent_substantive, recent_resolved, since}}``, where
    ``since`` is the oldest recent *open substantive* record's timestamp (the
    ledger is append-only chronological, so first seen is oldest) — the per-source
    signal coverage's degradation verdicts key on. Malformed ledger lines are
    counted as records but excluded from recency."""
    path = resolve_paths().home / LEDGER_FILE
    total = recent = recent_lines = recent_substantive = recent_resolved = 0
    by_source: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    # Closing records follow what they close, so the window is buffered and judged
    # at the end rather than decided line by line. Bounded by ``days``, not by the
    # ledger.
    window: list[tuple[dict, float]] = []
    resolved: dict[tuple[str, str], float] = {}
    for rec, at in _rows(path):
        if rec is None:
            total += 1  # unreadable, but it is a line someone wrote
            continue
        if _is_resolution(rec):
            _absorb_resolution(rec, resolved)
            continue
        total += 1
        if at is not None and at >= cutoff:
            window.append((rec, at))

    for rec, at in window:
        source = str(rec.get("source") or "")
        recent += 1
        try:
            recent_lines += int(rec.get("lines_skipped") or 0)
        except (TypeError, ValueError):
            pass
        per = by_source.setdefault(
            source,
            {"recent": 0, "recent_substantive": 0, "recent_resolved": 0, "since": None},
        )
        per["recent"] += 1
        if not _is_substantive(rec):
            continue
        sid = str(rec.get("source_id") or "")
        if at <= resolved.get((source, sid), float("-inf")):
            recent_resolved += 1
            per["recent_resolved"] += 1
            continue
        recent_substantive += 1
        per["recent_substantive"] += 1
        if per["since"] is None:
            per["since"] = rec.get("at")
    return {
        "total": total,
        "recent": recent,
        "recent_lines": recent_lines,
        "recent_substantive": recent_substantive,
        "recent_resolved": recent_resolved,
        "by_source": by_source,
        "days": days,
    }
