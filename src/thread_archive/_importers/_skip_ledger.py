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
say exactly which files to re-import (delete the watermark, re-run import) while
their sources still exist. The capture-coverage check summarizes recent volume.

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

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "capture-skips.jsonl"


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


def summarize_skips(*, days: float = 7.0) -> dict:
    """Ledger volume: total records ever, and records + lines within ``days``.
    Malformed ledger lines are counted as records but excluded from recency."""
    path = resolve_paths().home / LEDGER_FILE
    total = recent = recent_lines = 0
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
                        recent_lines += int(rec.get("lines_skipped") or 0)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return {"total": total, "recent": recent, "recent_lines": recent_lines, "days": days}
