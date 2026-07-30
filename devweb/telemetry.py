"""Developer telemetry survey — the data behind the ``/telemetry`` panel.

The archive's operational ledgers are intentionally separate: web requests,
ingest passes, retrieval calls, load runs, and ingest faults have different
producers and retention caps. This module assembles the parts a maintainer needs
to read together without turning those ledgers into a second metrics store.

The report is read-only and windowed. Detail comes from the two ledgers that do
not already have a dedicated web view: web request timings and ingest timings.
Retrieval and load runs are represented in the ledger inventory and keep their
deeper views at ``/retrieval`` here and ``/health`` on the archive's viewer.

``metrics`` is imported rather than owned: the web-request ledger's *producer*
is the viewer, which writes a row per request it serves. This reads that file.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from thread_archive._ops import ingest_errors, ledger, load_runs
from thread_archive._retrieval import usage
from thread_archive._watcher import ingest_log
from thread_archive._web import metrics


def _percentile(values: Iterable[float], q: float) -> float:
    """Nearest-rank percentile, rounded to the ledger's millisecond precision."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(-(-q * len(ordered) // 1)) - 1))
    return round(ordered[index], 1)


def _latencies(values: list[float]) -> dict[str, float | int]:
    """The compact latency band every web endpoint row carries."""
    return {
        "n": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": round(max(values), 1) if values else 0.0,
    }


def web_summary(home: Path, *, hours: int) -> dict[str, Any]:
    """Aggregate served requests over ``hours``, grouped by method and path.

    The ledger deliberately excludes query strings, so this report cannot expose
    search text. Errors are HTTP 4xx/5xx responses; contention is the number of
    rows that explicitly observed more than one concurrently served request.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "durations": [],
            "errors": 0,
            "bytes": 0,
            "concurrent": 0,
        }
    )
    durations: list[float] = []
    errors = 0
    response_bytes = 0
    concurrent = 0

    for row in ledger.iter_rows(home / metrics.LEDGER_FILE, newest_first=True):
        at = str(row.get("at") or "")
        if at and at < cutoff:
            break
        path = row.get("path")
        if not isinstance(path, str):
            continue
        method = str(row.get("method") or "GET")
        try:
            duration = float(row.get("duration_ms") or 0.0)
            status = int(row.get("status") or 0)
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        contended = bool(row.get("concurrent"))
        group = groups[(method, path)]
        group["durations"].append(duration)
        group["errors"] += int(status >= 400)
        group["bytes"] += size
        group["concurrent"] += int(contended)
        durations.append(duration)
        errors += int(status >= 400)
        response_bytes += size
        concurrent += int(contended)

    endpoints = []
    for (method, path), group in groups.items():
        endpoints.append({
            "method": method,
            "path": path,
            **_latencies(group.pop("durations")),
            **group,
        })
    endpoints.sort(key=lambda row: (-row["p95"], -row["n"], row["path"]))
    return {
        "requests": len(durations),
        "errors": errors,
        "bytes": response_bytes,
        "concurrent": concurrent,
        **{key: value for key, value in _latencies(durations).items() if key != "n"},
        "endpoints": endpoints,
        "retained_bytes": ledger.total_bytes(home / metrics.LEDGER_FILE),
    }


_LEDGERS = (
    (metrics.LEDGER_FILE, "web requests", "telemetry"),
    (ingest_log.LEDGER_FILE, "ingest work", "telemetry"),
    (usage.LEDGER_FILE, "retrieval calls", "retrieval"),
    (load_runs.LEDGER_FILE, "load runs", "health"),
    (ingest_errors.LEDGER_FILE, "ingest faults", "telemetry"),
)


def _ledger_inventory(home: Path) -> list[dict[str, Any]]:
    """Retained size and segment count for every operational telemetry ledger."""
    rows = []
    for filename, label, view in _LEDGERS:
        path = home / filename
        segments = ledger.segments(path)
        rows.append({
            "file": filename,
            "label": label,
            "view": view,
            "bytes": ledger.total_bytes(path),
            "segments": len(segments),
        })
    return rows


def report(home: Path, *, hours: int = 24) -> dict[str, Any]:
    """Assemble the developer telemetry report."""
    return {
        "home": str(home),
        "hours": hours,
        "at": datetime.now(timezone.utc).isoformat(),
        "web": web_summary(home, hours=hours),
        "ingest": ingest_log.summarize(home, hours=hours),
        "faults": ingest_errors.summarize(home),
        "ledgers": _ledger_inventory(home),
    }
