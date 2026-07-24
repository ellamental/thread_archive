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

Three record kinds, distinguished by ``kind``: ``search`` and ``read`` for the two
tools, and ``warm`` for one :func:`thread_archive._retrieval.warm_models` pass.
The warm row is here rather than in its own file because it is the other half of
the same latency story — the startup cost the model arms carry, recorded where it
is paid on purpose, against the cold flags that mark a request unlucky enough to
pay it inside the call.

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
    render_ms: Optional[float] = None,
    failed: bool = False,
    timings: Optional[dict[str, Any]] = None,
) -> None:
    """Record one ``thread_search`` call: the query, the non-default parameters,
    how many hits came back, the top result ids (``[event_id, thread_id]``
    pairs) for later join against reads, and the call's latency. ``hits`` is
    whatever the engine returned — result ids are extracted defensively, so a
    non-ranked output shape (count/linkable) records its parameters and count
    without ids.

    Latency comes in two numbers because they answer different questions.
    ``duration_ms`` is the retrieval work as the agent felt it (including a widen
    retry); ``render_ms`` is the formatting that turns those hits into the text the
    agent reads. Their sum is the tool call's wall-clock, and keeping them apart is
    what distinguishes a slow *search* from a slow *answer* — a wide result set can
    make the second large while the first is unchanged. ``render_ms`` is absent on a
    search that never reached the render.

    ``timings`` is the optional per-stage breakdown of ``duration_ms`` (the engine's
    :class:`thread_archive._retrieval._probe.SearchProbe` record — the three arm
    totals, the vector arm's sub-stages when it ran, ``did_rerank``, ``pool_size``,
    and the cold/``matrix_built`` flags when they apply). Total latency alone can't
    see which stage regressed; this makes the ledger self-diagnosing — still ids and
    timings only, never content.

    A search that raised is recorded too — the caller passes ``failed`` — with the
    time it burned before it did: an error that takes a minute to arrive is latency
    evidence, and dropping it would bias every percentile computed off this file
    toward the searches that happened to succeed. Stated by the caller rather than
    inferred from a missing ``render_ms``, so a surface that legitimately records no
    render (anything serving hits as data) isn't read as a failure."""
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
    if render_ms is not None:
        record["render_ms"] = round(render_ms, 1)
    if failed:
        record["failed"] = True
    if timings:
        record.update(timings)
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
    chars: Optional[int] = None,
    failed: bool = False,
) -> None:
    """Record one ``thread_read`` call: the id as the caller passed it (thread
    id, legacy integer id, or provider session uuid — searches log thread ids,
    so joins work for the id-from-search path) plus the non-default view
    parameters and the read's latency.

    ``chars`` is the size of what came back. A read's cost tracks how much
    conversation it materialized far more than which thread it opened, so latency
    without size is a distribution with its main explanatory variable missing — the
    reason a median read is milliseconds and the worst is seconds is mostly that
    they are not the same amount of work. Recorded as the denominator that makes the
    two comparable.

    ``failed`` marks a read that raised, for the same reason searches record it: the
    slow failures are evidence, and dropping them flatters every percentile."""
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
    if chars is not None:
        record["chars"] = chars
    if failed:
        record["failed"] = True
    _append(record)


def record_warm(
    *,
    duration_ms: float,
    stages: dict[str, float],
    failed: Optional[list[str]] = None,
) -> None:
    """Record one :func:`thread_archive._retrieval.warm_models` pass — how long a
    process took to become useful, split by stage (``embed_ms``, ``rerank_ms``,
    ``graph_ms``, ``search_ms``).

    A ``warm`` row is the counterpart to the cold flags on a search: those say a
    request paid a load, this says what the load costs when it is paid where it
    should be. Together they answer the question neither can alone — whether a
    slow first search means warming is broken or merely that a query arrived
    before it finished. ``failed`` names the stages that raised; a warm pass is
    best-effort, so a partial one is normal and worth distinguishing from a
    complete one that was simply slow."""
    if not _enabled():
        return
    record: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "warm",
        "duration_ms": round(duration_ms, 1),
    }
    record.update({k: round(v, 1) for k, v in stages.items()})
    if failed:
        record["failed_stages"] = failed
    _append(record)
