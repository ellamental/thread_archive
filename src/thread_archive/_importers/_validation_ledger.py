"""The parse-validation ledger: ``<home>/validation-drift.jsonl``.

Import-time validation (:func:`.._importers._events.log_parse_validation`) logs
each finding, but a log line is ephemeral — the operator surface for "is a parser
drifting?" needs a durable, queryable trail. Every import whose parsed messages
tripped a validator appends one record here: the provider, the source id, and the
findings (a block type or role the parser has gone blind to, a missing field).
:func:`summarize_drift` gives the capture-coverage check its recent-volume line, so
drift shows up in ``thread-archive coverage`` and the nightly's coverage stage rather than
only in daemon logs.

Not every record is evidence of drift. The version tripwire
(:mod:`._versions`) writes *advisory* records — a harness released a version
never seen before, which is a heads-up, not a finding — and those are held out
of ``recent_substantive``, the count the coverage warning and the per-provider
degradation verdict key on. A harness that ships a release most days would
otherwise sit permanently degraded on advisories alone, which buries the real
signal when a field finally does drift.

Sibling to the capture-skip ledger — the same concern (provider format drift)
caught a different way: a skip is source lines consumed *unimported*; drift is
content that *was* imported but a validator flagged its shape. Append-only JSONL,
advisory, fail-soft — a ledger write must never break the import it describes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

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


def summarize_drift(*, days: float = 7.0) -> dict:
    """Ledger volume: total records ever, and records + findings within ``days``.
    ``recent_substantive`` and ``recent_substantive_findings`` are the subset of
    recent records that are not advisory (see :func:`_is_advisory`) and their
    findings — what the coverage warning fires on and counts, so a harness
    bumping its version most days doesn't cry wolf. ``by_provider``
    breaks the recent window down per provider —
    ``{provider: {recent, recent_substantive, recent_findings, since}}``, where
    ``since`` is the oldest recent *substantive* record's timestamp (the ledger
    is append-only chronological, so first seen is oldest) — the per-source
    signal coverage's degradation verdicts key on. Malformed ledger lines are
    counted as records but excluded from recency."""
    path = resolve_paths().home / LEDGER_FILE
    total = recent = recent_findings = recent_substantive = 0
    recent_substantive_findings = 0
    by_provider: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                    at = datetime.fromisoformat(rec["at"])
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    if at.timestamp() >= cutoff:
                        recent += 1
                        recent_findings += int(rec.get("count") or 0)
                        per = by_provider.setdefault(
                            str(rec.get("provider") or ""),
                            {"recent": 0, "recent_substantive": 0,
                             "recent_findings": 0, "since": None},
                        )
                        per["recent"] += 1
                        per["recent_findings"] += int(rec.get("count") or 0)
                        if not _is_advisory(rec):
                            recent_substantive += 1
                            recent_substantive_findings += int(rec.get("count") or 0)
                            per["recent_substantive"] += 1
                            if per["since"] is None:
                                per["since"] = rec["at"]
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return {
        "total": total,
        "recent": recent,
        "recent_substantive": recent_substantive,
        "recent_substantive_findings": recent_substantive_findings,
        "recent_findings": recent_findings,
        "by_provider": by_provider,
        "days": days,
    }
