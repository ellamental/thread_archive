"""The patch-lifecycle ledger: ``<home>/patch-log.jsonl``.

Every override patch's lifecycle transition — scaffolded, activated, pinned,
unpinned, retired — appends one record here. The audit trail the drift ledgers
promise a fix will leave behind: months later, "why is codex import shaped
differently on this machine, and since when?" is answered by this file plus the
patch's own directory, not by archaeology over config.json's git-less history.

Sibling in spirit to the capture-skip and validation-drift ledgers: append-only
JSONL, advisory, fail-soft — a ledger write must never break the operation it
describes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "patch-log.jsonl"


def record_patch_event(
    event: str, provider: str, *, home: Optional[str] = None, **fields: object
) -> None:
    """Append one lifecycle record: ``event`` names the transition
    (``scaffolded`` / ``activated`` / ``pinned`` / ``unpinned`` / ``retired``),
    extra ``fields`` carry its specifics (versions, counts, reasons)."""
    try:
        path = resolve_paths(home).home / LEDGER_FILE
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "provider": provider,
            **fields,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.warning(
            "could not record patch event %s for %s", event, provider, exc_info=True
        )
