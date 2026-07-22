"""The retrieval-usage ledger: ``<home>/retrieval-usage.jsonl``.

Retrieval quality has exactly one honest ground truth: what agents actually
search for and which results they go on to read. This ledger captures that
real task — every ``thread_search`` and ``thread_read`` served by the MCP
surface — so evals (and the knowledge-layer verdict) can be built from
observed behaviour instead of intuition: it is the sampling frame of real
query shapes the gold miner draws from. A read joins to the searches before it
by thread id.

Records hold query text, filter parameters, result *ids*, and the call's
wall-clock latency (``duration_ms``) — never event content, snippets, or
transcripts — so redaction never needs to touch this file, and a leaked ledger
names conversations without quoting them. Latency rides along because it is
the one regression class result-quality evals can't see: a search that returns
the right hits ever slower looks perfect until someone measures. The file
lives beside the other home-root ledgers (``capture-skips.jsonl``,
``validation-drift.jsonl``), outside ``truth/`` — it is operational telemetry,
not archive data, and no backup/verify path depends on it.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the
retrieval call it describes. ``THREAD_ARCHIVE_USAGE_LOG=0`` disables it. The
file self-rotates: at ``max_bytes()`` the current file is renamed to
``retrieval-usage.jsonl.1`` (replacing any previous rotation) and a fresh file
starts — bounded disk, and at observed agent volumes the window still spans
months.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .._config import resolve_paths

logger = logging.getLogger(__name__)

LEDGER_FILE = "retrieval-usage.jsonl"

_MAX_RESULT_IDS = 20  # per-search result ids retained — enough to judge rank quality


def max_bytes() -> int:
    """Size at which the ledger rotates to ``.jsonl.1`` (32 MB by default).

    Read per call from ``THREAD_ARCHIVE_USAGE_MAX_BYTES``, like ``_enabled()``
    beside it: a constant would answer once at import and ignore any later word
    on it.
    """
    return int(os.environ.get("THREAD_ARCHIVE_USAGE_MAX_BYTES") or 32 * 1024 * 1024)


def _enabled() -> bool:
    return os.environ.get("THREAD_ARCHIVE_USAGE_LOG", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _append(record: dict) -> None:
    """Append one record, rotating first when the file is at cap. Fail-soft."""
    try:
        path = resolve_paths().home / LEDGER_FILE
        try:
            if path.stat().st_size >= max_bytes():
                path.replace(path.with_suffix(".jsonl.1"))
        except FileNotFoundError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("could not record retrieval usage", exc_info=True)


def record_search(
    query: str,
    *,
    params: dict[str, Any],
    hits: object,
    widened: bool,
    duration_ms: Optional[float] = None,
) -> None:
    """Record one ``thread_search`` call: the query, the non-default parameters,
    how many hits came back, the top result ids (``[event_id, thread_id]``
    pairs) for later join against reads, and the call's latency. ``hits`` is
    whatever the engine returned — result ids are extracted defensively, so a
    non-ranked output shape (count/linkable) records its parameters and count
    without ids. ``duration_ms`` covers the retrieval work as the agent felt it
    (including a widen retry), not ledger/render overhead."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "search",
        "query": query,
    }
    record.update({k: v for k, v in params.items() if v is not None})
    if widened:
        record["widened"] = True
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    results: list[list[int | str]] = []
    if isinstance(hits, list):
        record["n_hits"] = len(hits)
        for hit in hits[:_MAX_RESULT_IDS]:
            if isinstance(hit, dict) and "event_id" in hit and "thread_id" in hit:
                try:
                    results.append([int(hit["event_id"]), str(hit["thread_id"])])
                except (TypeError, ValueError):
                    continue
    if results:
        record["results"] = results
    _append(record)


def record_read(
    thread_id: object,
    *,
    params: Optional[dict[str, Any]] = None,
    duration_ms: Optional[float] = None,
) -> None:
    """Record one ``thread_read`` call: the id as the caller passed it (thread
    id, legacy integer id, or provider session uuid — searches log thread ids,
    so joins work for the id-from-search path) plus the non-default view
    parameters and the read's latency."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "read",
        "thread_id": thread_id if isinstance(thread_id, int) else str(thread_id),
    }
    if params:
        record.update({k: v for k, v in params.items() if v not in (None, False, 0)})
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    _append(record)
