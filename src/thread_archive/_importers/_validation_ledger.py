"""The parse-validation ledger: ``<home>/validation-drift.jsonl``.

Import-time validation (:func:`.._importers._events.log_parse_validation`) logs
each finding, but a log line is ephemeral — the operator surface for "is a parser
drifting?" needs a durable, queryable trail. Every import whose parsed messages
tripped a validator appends one record here: the provider, the source id, and the
findings (a block type or role the parser has gone blind to, a missing field).
:func:`summarize_drift` gives the capture-coverage check its recent-volume line, so
drift shows up in ``thread-archive source coverage`` and the nightly's coverage stage rather than
only in daemon logs.

Not every record is evidence of drift. The version tripwire
(:mod:`._versions`) writes *advisory* records — a harness released a version
never seen before, which is a heads-up, not a finding — and those are held out
of ``recent_substantive``, the count the coverage warning and the per-provider
degradation verdict key on. A harness that ships a release most days would
otherwise sit permanently degraded on advisories alone, which buries the real
signal when a field finally does drift.

**A record is an observation, and a fix closes it rather than erasing it.** The
ledger is append-only in both directions: a repair cannot delete the drift it
repaired, and must not — what a parser once got wrong is the trail a later
regression is read against. What a repair *can* do is append a **resolution**
(:func:`record_resolution`) naming the ``source_id``s it re-read and the moment
it started. Records at or before that moment drop out of ``recent_substantive``
while staying in ``total`` and ``recent``, so the degradation verdict that told
the operator to repair stops telling them once the repair is done, and the
history of what happened is undiminished. Without this the verdict outlives its
cause by the whole rolling window, advising a repair that has already run.

Closed by evidence, not by assertion. The only writer of a resolution is
:mod:`.._repair.reimport`, and the only thing it closes is a file it actually
re-read through the current parser without the finding coming back. The stamp is
taken *before* the re-read, so findings the re-parse itself records land after it
and stay open: a repair that did not work closes nothing.

Sibling to the capture-skip ledger — the same concern (provider format drift)
caught a different way: a skip is source lines consumed *unimported*; drift is
content that *was* imported but a validator flagged its shape. Append-only JSONL,
advisory, fail-soft — a ledger write must never break the import it describes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "validation-drift.jsonl"
# Per record the count stays exact; the stored sample is bounded so a pathological
# conversation can't write a multi-megabyte ledger line.
_MAX_FINDINGS = 20

# The opening words of a version-tripwire finding. Shared with :mod:`._versions`,
# which builds its findings from it, so the writer and :func:`_is_advisory` can
# never disagree about what an advisory looks like.
VERSION_SIGHTING_LEAD = "First sighting of"

#: Marks a closing record. An observation carries no ``kind`` — the shape every
#: writer used before resolutions existed, and the shape a reader falls back to,
#: so an old ledger reads correctly and an old reader ignores what it can't use.
RESOLUTION_KIND = "resolution"


def record_drift(
    provider: str,
    source_id: str,
    *,
    findings: list[str],
    batch_safe: bool,
    advisory: bool = False,
) -> None:
    """Append one record for an import whose messages tripped ≥1 validator.

    ``batch_safe`` rides along so the operator can tell incremental format-drift
    (the high-value "parser went blind" signal, all that a slice can raise) from a
    full account-export's richer findings. ``advisory`` marks a record that is a
    heads-up rather than a finding — ledgered for the trail, but held out of the
    substantive counts coverage warns and degrades on. A no-finding import
    records nothing."""
    if not findings:
        return
    try:
        path = resolve_paths().home / LEDGER_FILE
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "source_id": source_id,
            "batch_safe": bool(batch_safe),
            "advisory": bool(advisory),
            "count": len(findings),
            "findings": findings[:_MAX_FINDINGS],
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning(
            "could not record validation drift for %s:%s", provider, source_id, exc_info=True
        )


def _is_advisory(rec: dict) -> bool:
    """Whether a ledger record is a heads-up rather than a validator finding.

    The ``advisory`` flag on the record is the answer when it carries one. The
    ledger is append-only and outlives any one writer, so a record without the
    flag is classified by its findings instead: an all-version-sighting record
    is advisory by construction, anything else is substantive. Reading falls
    toward substantive — an unrecognized record is drift until proven otherwise.
    """
    flag = rec.get("advisory")
    if isinstance(flag, bool):
        return flag
    findings = rec.get("findings")
    if not isinstance(findings, list) or not findings:
        return False
    return all(str(f).startswith(VERSION_SIGHTING_LEAD) for f in findings)


def _epoch(value: object) -> Optional[float]:
    """An ISO timestamp as a UTC epoch, or ``None`` if it isn't one. Naive stamps
    are read as UTC — every writer here emits UTC, and the alternative is
    discarding a record over a missing suffix."""
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp()


def _rows(path: Path) -> Iterator[tuple[Optional[dict], Optional[float]]]:
    """Every line of the ledger as ``(record, epoch)``. A line that doesn't parse
    yields ``(None, None)`` rather than raising or vanishing: callers count it as
    a record they can't read, which is what it is. A ledger that doesn't exist
    yields nothing — a young archive has no drift, not an error."""
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
    """Fold one closing record into ``{(provider, source_id): latest through}``.
    Latest wins: an id closed twice is closed as of the later repair."""
    through = _epoch(rec.get("through"))
    if through is None:
        return
    provider = str(rec.get("provider") or "")
    ids = rec.get("source_ids")
    if not isinstance(ids, list):
        return
    for sid in ids:
        key = (provider, str(sid))
        if through > into.get(key, float("-inf")):
            into[key] = through


def _findings(rec: dict) -> int:
    try:
        return int(rec.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _open_records(
    path: Path, provider: str, source_ids: set[str], through: float
) -> int:
    """How many substantive records for ``provider`` name one of ``source_ids``,
    predate ``through``, and are not already closed."""
    candidates: list[tuple[str, float]] = []
    resolved: dict[tuple[str, str], float] = {}
    for rec, at in _rows(path):
        if rec is None:
            continue
        if _is_resolution(rec):
            _absorb_resolution(rec, resolved)
            continue
        if at is None or at > through or _is_advisory(rec):
            continue
        if str(rec.get("provider") or "") != provider:
            continue
        sid = str(rec.get("source_id") or "")
        if sid in source_ids:
            candidates.append((sid, at))
    return sum(
        1 for sid, at in candidates if at > resolved.get((provider, sid), float("-inf"))
    )


def record_resolution(
    provider: str, source_ids: Iterable[str], *, through: datetime, by: str
) -> int:
    """Close ``provider``'s drift records for ``source_ids`` dated at or before
    ``through``. Returns how many records that closes; writes nothing when zero.

    ``through`` belongs to the caller and must be stamped *before* the re-parse
    that justifies the closure — that ordering is the entire guarantee. Findings
    the re-parse records land after the stamp, stay open, and keep the source
    degraded, so this cannot be used to declare a fix that isn't one.

    Nothing is rewritten or removed: the closed observations stay in the file and
    in ``total``/``recent``. Only their standing as *current* evidence ends.
    """
    ids = {str(s) for s in source_ids if str(s)}
    if not ids:
        return 0
    path = resolve_paths().home / LEDGER_FILE
    closing = _open_records(path, provider, ids, through.timestamp())
    if not closing:
        return 0  # nothing open to close: a repeat recheck adds no noise
    try:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": RESOLUTION_KIND,
            "provider": provider,
            "source_ids": sorted(ids),
            "through": through.isoformat(),
            "by": by,
            "closed": closing,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning("could not record drift resolution for %s", provider, exc_info=True)
        return 0
    return closing


def substantive_since(provider: str, since: datetime) -> set[str]:
    """``source_id``s with a substantive drift record newer than ``since`` — the
    files the current parser is *still* stumbling on, as of a repair that started
    then. Advisory records are not evidence and never appear here."""
    cutoff = since.timestamp()
    out: set[str] = set()
    for rec, at in _rows(resolve_paths().home / LEDGER_FILE):
        if rec is None or at is None or at <= cutoff:
            continue
        if _is_resolution(rec) or _is_advisory(rec):
            continue
        if str(rec.get("provider") or "") != provider:
            continue
        sid = str(rec.get("source_id") or "")
        if sid:
            out.add(sid)
    return out


def summarize_drift(*, days: float = 7.0) -> dict:
    """Ledger volume: total records ever, and records + findings within ``days``.
    ``recent_substantive`` and ``recent_substantive_findings`` are the subset of
    recent records that are neither advisory (see :func:`_is_advisory`) nor closed
    by a resolution (see :func:`record_resolution`) — what the coverage warning
    fires on and counts, so neither a harness bumping its version most days nor a
    drift already repaired cries wolf. ``recent_resolved`` counts the closed ones,
    which is what makes a repaired-but-still-remembered source legible rather than
    just quiet. ``by_provider`` breaks the recent window down per provider —
    ``{provider: {recent, recent_substantive, recent_findings, recent_resolved,
    since}}``, where ``since`` is the oldest recent *open substantive* record's
    timestamp (the ledger is append-only chronological, so first seen is oldest) —
    the per-source signal coverage's degradation verdicts key on. Malformed ledger
    lines are counted as records but excluded from recency."""
    path = resolve_paths().home / LEDGER_FILE
    total = recent = recent_findings = recent_substantive = 0
    recent_substantive_findings = recent_resolved = 0
    by_provider: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    # Resolutions follow the observations they close, so recency can't be decided
    # in one forward pass — the window is buffered and judged at the end. Bounded
    # by ``days`` of records, not by the ledger, which grows without limit.
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
        provider = str(rec.get("provider") or "")
        found = _findings(rec)
        recent += 1
        recent_findings += found
        per = by_provider.setdefault(
            provider,
            {"recent": 0, "recent_substantive": 0, "recent_findings": 0,
             "recent_resolved": 0, "since": None},
        )
        per["recent"] += 1
        per["recent_findings"] += found
        if _is_advisory(rec):
            continue
        sid = str(rec.get("source_id") or "")
        if at <= resolved.get((provider, sid), float("-inf")):
            recent_resolved += 1
            per["recent_resolved"] += 1
            continue
        recent_substantive += 1
        recent_substantive_findings += found
        per["recent_substantive"] += 1
        if per["since"] is None:
            per["since"] = rec.get("at")
    return {
        "total": total,
        "recent": recent,
        "recent_substantive": recent_substantive,
        "recent_substantive_findings": recent_substantive_findings,
        "recent_findings": recent_findings,
        "recent_resolved": recent_resolved,
        "by_provider": by_provider,
        "days": days,
    }
