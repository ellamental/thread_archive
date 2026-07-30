"""The ingest ledger: ``<home>/ingest-runs.jsonl``.

Retrieval has had a per-call record with a stage breakdown for a long time; the
ingest half of the archive has had a cumulative counter per source in
``health.json`` and nothing else. That asymmetry is backwards for a capture
product: ingest is the side that runs continuously, the side whose cost grows
with the corpus, and the side where a slow regression is invisible until the
watcher is visibly behind.

The gap is not that ingest was unmeasured but that it was un-*retained*. The
health record holds totals since process start, so a restart erases them, and it
is throttled to one write per five minutes, so it samples whichever pass happened
to be running. Neither shape can answer "when did this get slow" — the question
every ingest complaint reduces to.

One row per poll pass **that did work**, carrying the
:class:`~.._importers._probe.IngestProbe` stage split, the source it polled, the
volume it moved, and the wall time the loop actually waited. A pass that
fingerprint-skipped every target writes nothing: the loop spends most of its life
finding nothing to do, and rows for that would bury the ones that matter under
millions of no-ops.

``pass_ms`` against the probe's ``total_ms`` is the row's own consistency check.
The probe sums the stages an import ran; ``pass_ms`` is the wall clock around the
whole source poll. The difference is what the loop spent *not* importing —
directory walks, fingerprint stats, the targets it skipped — and on a source with
many files and few changes that difference is the entire cost.

Append-only JSONL, advisory, fail-soft — a ledger write must never break the
ingest it describes. Recorded only on an install being developed on
(:mod:`.._ops.telemetry`); ``THREAD_ARCHIVE_INGEST_LOG`` overrides either way.
Rotation retains every segment (see :mod:`.._ops.ledger`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .._ops import ledger, telemetry

logger = logging.getLogger(__name__)

LEDGER_FILE = "ingest-runs.jsonl"


def max_bytes() -> int:
    """Size at which a segment rotates (16 MB by default)."""
    return ledger.env_max_bytes("THREAD_ARCHIVE_INGEST_MAX_BYTES", 16 * 1024 * 1024)


def enabled(home=None) -> bool:
    """Whether this install records what ingest cost.

    Off unless the install is being developed on (:mod:`.._ops.telemetry`);
    ``THREAD_ARCHIVE_INGEST_LOG`` overrides in either direction.

    Every writer here tests its "nothing to say" condition *before* asking this,
    so the poll loop's common case — a pass that fingerprint-skipped every target —
    still costs one attribute read rather than a config file.
    """
    return telemetry.recording("THREAD_ARCHIVE_INGEST_LOG", home)


def record_pass(
    source: str,
    *,
    home,
    probe: Any,
    pass_ms: float,
    result: Any = None,
    lag_s: Optional[float] = None,
) -> None:
    """Append one source's poll, with its stage split. Never raises.

    Skipped entirely when the probe reports no work: see the module docstring —
    the quiet loop is the common case and it has nothing to say.

    ``errors`` rides along as a count rather than as text. The messages already
    have a home (``watch_errors_last``, and the log); what this ledger needs from
    them is only whether the pass's timings describe a clean import or a failing
    one, since work that fails slowly skews every percentile computed here.
    """
    try:
        if probe is None or not probe.ran:
            return
        if not enabled(home):
            return
        record: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "ingest-pass",
            "source": source,
            "pass_ms": round(pass_ms, 1),
        }
        record.update(probe.as_record())
        if result is not None:
            errors = len(getattr(result, "errors", ()) or ())
            if errors:
                record["errors"] = errors
            parse_errors = getattr(result, "parse_errors", 0) or 0
            if parse_errors:
                record["parse_errors"] = parse_errors
        if lag_s is not None:
            record["lag_s"] = lag_s
        ledger.append(home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — advisory; the poll loop must survive
        logger.debug("could not record ingest pass", exc_info=True)


def record_idle(
    *,
    home,
    passes: int,
    total_ms: float,
    max_ms: float,
    window_s: float,
    checked: int,
    load1_avg: Optional[float] = None,
) -> None:
    """Append one rollup of the passes that found nothing to do. Never raises.

    A pass that fingerprint-skips every target writes no ``ingest-pass`` row, and
    that is right: the loop spends nearly all of its life finding nothing, and a
    row each would bury every row that matters. But it leaves the loop's *floor*
    unrecorded — the directory walks and fingerprint stats paid on every poll
    whether or not anything changed. That cost is real, it scales with the number
    of watched files rather than with activity, and until now it lived only in
    ``health.json``'s per-source counters, which are cumulative since process
    start and therefore erased by every restart. The same un-retention this
    ledger was built to fix, for the quiet half of the loop.

    So: one row per window, not per pass — ``passes`` of them cost ``total_ms``
    between them, the worst taking ``max_ms``, over ``checked`` targets. Rate per
    pass is the number to read (``total_ms / passes``); the window length is here
    so a reader can tell a busy interval from a sleepy one rather than assuming a
    poll cadence that config controls.

    ``max_ms`` because the mean hides the case worth catching: a loop that is
    usually instant and occasionally stalls for seconds on a cold directory
    averages out to healthy.

    ``load1_avg`` is the machine averaged across the window's passes, and without
    it the rest of the row cannot be read. A pass costs what it costs partly
    because of how many files it has to stat and partly because of what else the
    box was doing, and those want opposite responses: the first is the loop
    getting expensive, the second is an afternoon. Sampled per pass rather than
    once at flush, because a spot reading at the end of five minutes describes the
    end of five minutes.
    """
    try:
        if passes <= 0:
            return
        if not enabled(home):
            return
        record: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "idle",
            "passes": passes,
            "total_ms": round(total_ms, 1),
            "max_ms": round(max_ms, 1),
            "window_s": round(window_s, 1),
            "checked": checked,
        }
        if load1_avg is not None:
            record["load1_avg"] = round(load1_avg, 2)
        ledger.append(home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — advisory; the poll loop must survive
        logger.debug("could not record idle rollup", exc_info=True)


def _percentile(xs: list[float], q: float) -> float:
    """Nearest-rank quantile — the honest one at these sample counts: an
    interpolated p95 over forty passes invents a duration no pass took."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    idx = max(0, min(len(ordered) - 1, int(-(-q * len(ordered) // 1)) - 1))
    return ordered[idx]


def summarize(home, *, hours: int = 24) -> dict[str, Any]:
    """What ingest cost over the window, per source and per stage.

    Reads every retained segment, so the window is the only thing bounding it.
    Per source: how many passes did work, what they moved, and the p50/p95 of
    what one pass cost. Per stage: the total milliseconds spent there across the
    window — which is the number that actually ranks them, since a stage that is
    never individually slow can still be where the day went.

    Maintenance and embed rows are summarized beside the source rows rather than
    mixed into them: they are the loop's other two jobs, and folding them into a
    source's totals would attribute upkeep to whichever provider happened to
    trigger it. The same for idle rollups, which are the loop's *fourth* job and
    the one nothing else reports: what it costs to keep finding nothing."""
    from datetime import timedelta

    from .._importers._probe import STAGES
    from .._ops import ledger

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    by_source: dict[str, dict[str, Any]] = {}
    stage_ms: dict[str, float] = {}
    maintenance: list[float] = []
    embed: list[float] = []
    embedded = 0
    idle_passes = 0
    idle_ms = 0.0
    idle_max_ms = 0.0

    for row in ledger.iter_rows(home / LEDGER_FILE):
        if row.get("at", "") < cutoff:
            continue
        kind = row.get("kind")
        if kind == "maintenance":
            maintenance.append(float(row.get("ms") or 0.0))
            for name in ("lock_ms", "checkpoint_ms", "thread_meta_ms", "code_ms",
                         "rebalance_ms", "threads_ms", "snapshot_ms",
                         "import_state_ms", "manifest_ms"):
                if row.get(name):
                    stage_ms[name] = stage_ms.get(name, 0.0) + float(row[name])
            continue
        if kind == "embed":
            embed.append(float(row.get("ms") or 0.0))
            embedded += int(row.get("embedded") or 0)
            continue
        if kind == "idle":
            idle_passes += int(row.get("passes") or 0)
            idle_ms += float(row.get("total_ms") or 0.0)
            idle_max_ms = max(idle_max_ms, float(row.get("max_ms") or 0.0))
            continue
        if kind != "ingest-pass":
            continue
        src = by_source.setdefault(str(row.get("source")), {
            "passes": 0, "items": 0, "events": 0, "lines": 0, "bytes": 0,
            "errors": 0, "_pass_ms": [],
        })
        src["passes"] += 1
        for counter in ("items", "events", "lines", "bytes"):
            src[counter] += int(row.get(counter) or 0)
        src["errors"] += int(row.get("errors") or 0)
        src["_pass_ms"].append(float(row.get("pass_ms") or 0.0))
        for name in STAGES:
            if row.get(name):
                stage_ms[name] = stage_ms.get(name, 0.0) + float(row[name])

    sources = {}
    for name, s in sorted(by_source.items()):
        times = s.pop("_pass_ms")
        sources[name] = {
            **s,
            "pass_p50_ms": round(_percentile(times, 0.50), 1),
            "pass_p95_ms": round(_percentile(times, 0.95), 1),
            "total_s": round(sum(times) / 1000.0, 1),
        }
    return {
        "hours": hours,
        "sources": sources,
        "stages": dict(sorted(stage_ms.items(), key=lambda kv: -kv[1])),
        "maintenance": {"passes": len(maintenance),
                        "total_s": round(sum(maintenance) / 1000.0, 1),
                        "p95_ms": round(_percentile(maintenance, 0.95), 1)},
        "embed": {"passes": len(embed), "embedded": embedded,
                  "total_s": round(sum(embed) / 1000.0, 1),
                  "p95_ms": round(_percentile(embed, 0.95), 1)},
        # The loop's floor. ``per_pass_ms`` is the number to watch: totals grow
        # with how long the daemon has been up, and the per-pass cost grows with
        # how many files it has to look at — only the second is a regression.
        "idle": {"passes": idle_passes,
                 "total_s": round(idle_ms / 1000.0, 1),
                 "max_ms": round(idle_max_ms, 1),
                 "per_pass_ms": round(idle_ms / idle_passes, 1) if idle_passes else 0.0},
        "retained_bytes": ledger.total_bytes(home / LEDGER_FILE),
    }


def record_maintenance(*, home, timings: dict[str, float], counts: dict) -> None:
    """Append one maintenance pass's sub-timings.

    The health record carries the same split but only for the *last* pass, so a
    rebalance that has been getting slower for a week is invisible there. Here it
    is a series."""
    if not enabled(home):
        return
    try:
        record: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "maintenance",
        }
        record.update({k: round(v, 1) for k, v in timings.items()})
        record["counts"] = {k: v for k, v in counts.items() if isinstance(v, int)}
        ledger.append(home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — advisory; the poll loop must survive
        logger.debug("could not record maintenance pass", exc_info=True)


def record_embed(*, home, embedded: int, elapsed_ms: float, detail_ms: dict[str, float],
                 pending: int = 0, capped: bool = False) -> None:
    """Append one embed-cohost drain, with its select/model_load/encode/write split.

    The drain is the slowest steady-state work the daemon does and the one whose
    backlog is invisible from outside — a stopped drain and a caught-up one both
    embed nothing per pass. Recorded on every pass that embedded something, plus
    every pass that found a backlog it could not clear, since a drain falling
    behind is exactly the row a later question needs."""
    try:
        if not embedded and not pending:
            return
        if not enabled(home):
            return
        record: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "embed",
            "embedded": embedded,
            "pending": pending,
            "ms": round(elapsed_ms, 1),
        }
        if capped:
            record["capped"] = True
        record.update({k: round(v, 1) for k, v in detail_ms.items()})
        ledger.append(home / LEDGER_FILE, record, max_bytes=max_bytes())
    except Exception:  # noqa: BLE001 — advisory; the poll loop must survive
        logger.debug("could not record embed pass", exc_info=True)
