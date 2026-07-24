"""The web viewer's request ledger: ``<home>/web-requests.jsonl``.

The viewer is a real read surface over the same index the MCP tools use — it runs
searches, hydrates threads, and computes surveys — and it is the one surface whose
latency nobody has ever recorded. A page that takes twenty seconds and a page that
takes two hundred milliseconds look the same from the outside: both eventually
render.

Deliberately its own file rather than rows in ``retrieval-usage.jsonl``. That
ledger is the observed ground truth future retrieval evals are built from — an
agent's queries and the reads that followed them — and browsing is a different
behavior with different intent. Mixing a human clicking around into the set of
"queries an agent asked" would quietly bias every eval mined from it.

Endpoint and outcome only: path, status, response size, wall time. Query strings
are left out — the path alone answers which endpoint is slow, and the ledger has
no reason to accumulate whatever anyone typed into the search box.

Advisory and fail-soft throughout, like every other telemetry writer here: a
metrics write must never break the request it describes.
``THREAD_ARCHIVE_WEB_METRICS=0`` disables it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "web-requests.jsonl"


def max_bytes() -> int:
    """Size at which the ledger rotates to ``.jsonl.1`` (8 MB by default).

    Smaller than the retrieval ledger's cap: these rows are small and a browsing
    session makes a great many of them, and unlike search usage they have no second
    life as eval material — the recent distribution is the whole value.
    """
    return int(os.environ.get("THREAD_ARCHIVE_WEB_METRICS_MAX_BYTES") or 8 * 1024 * 1024)


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_WEB_METRICS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def record_request(
    path: str,
    *,
    status: int,
    duration_ms: float,
    size: Optional[int] = None,
) -> None:
    """Append one served request. Never raises."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    if size is not None:
        record["size"] = size
    try:
        p = resolve_paths().home / LEDGER_FILE
        try:
            if p.stat().st_size >= max_bytes():
                p.replace(p.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record web request metrics", exc_info=True)
