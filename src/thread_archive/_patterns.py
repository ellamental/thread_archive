"""Corpus-wide behavioral sequence mining for the patterns viewer.

The conversation event log is already a sequence database: a thread is one trace,
and its ordered events are the observations.  This module projects the noisy raw
event vocabulary into two useful alphabets and mines recurring, bounded-gap
subsequences from both:

``shape``
    Provider-independent behavior (user message, tool call, tool error, thinking,
    context summary, ...).

``detail``
    The same behavior with canonical tool names retained, so a broad structural
    pattern and its concrete Bash/Edit/MCP variants can both surface.

This is intentionally an explicit batch operation. Mining millions of events is
operator work, not latency a page request should spring on the watcher. The CLI
writes a report and pageable match index under ``<home>/experiments/patterns``;
the web endpoint reads those derived artifacts and reports how many events have
landed since their watermark. They are disposable and never part of the JSONL truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import bindparam, text

from ._config import resolve_paths
from ._store import get_engine

REPORT_VERSION = 2
DEFAULT_THREAD_TYPES = ("conversation", "system")
DEFAULT_MIN_SUPPORT = 10
DEFAULT_MAX_LENGTH = 3
DEFAULT_MAX_GAP = 2
DEFAULT_MAX_PATTERNS = 240
EXAMPLES_PER_PATTERN = 3

# Transport/machinery events vastly outnumber behavior and create no useful motif.
# api_request_completed is retained specially: it stands in for assistant content
# only when a streaming source emitted no granular text/thinking twin for the call.
_IGNORED_EVENT_TYPES = (
    "api_request_started",
    "stream_completed",
    "text_delta",
    "thinking_delta",
    "progress",
    "file_snapshot",
    "content_block",
    "tool_execution_started",
    "archived_duplicate",
)


@dataclass(frozen=True)
class _Activity:
    """One projected event, carrying both mining alphabets and its source anchor."""

    shape: str
    detail: str
    event_id: int
    occurred_at: str


@dataclass
class _Trace:
    thread_id: str
    title: str | None
    source: str | None
    thread_type: str
    updated_at: str | None
    activities: list[_Activity]


@dataclass(frozen=True)
class _PendingCall:
    """A call placeholder that will become its paired outcome when one arrives."""

    position: int
    name: str
    signature: str | None
    event_id: int


def _display_event(event_type: str) -> str:
    return event_type.replace("_", " ")


def _tool_ids(kind: str, name: str | None, category: str | None = None) -> tuple[str, str]:
    clean = (name or "unknown").strip()[:200] or "unknown"
    suffix = f":{category}" if category else ""
    return f"tool:{kind}", f"tool:{kind}:{clean}{suffix}"


def _activity_meta(activity_id: str, abstraction: str) -> dict:
    """Turn a compact stable id into viewer-ready vocabulary metadata."""
    parts = activity_id.split(":")
    if parts[0] == "tool":
        kind = parts[1]
        tool = parts[2] if len(parts) > 2 else None
        category = " ".join(parts[3:]).replace("_", " ") if len(parts) > 3 else None
        verb = {
            "call": "call",
            "success": "success",
            "error": "error",
            "recovery_changed": "recovered · changed args",
            "recovery_same": "recovered · same args",
            "recovery": "recovered",
        }.get(kind, kind.replace("_", " "))
        label = f"{verb} · {tool}" if tool else f"tool {verb}"
        if category:
            label += f" · {category}"
        return {
            "id": activity_id,
            "kind": f"tool_{kind}",
            "detail": " · ".join(filter(None, (tool, category))) or None,
            "label": label,
        }
    label = activity_id.replace(":", " ").replace("-", " ")
    return {"id": activity_id, "kind": parts[0], "detail": None, "label": label}


_EVENT_QUERY = text(
    f"""
    SELECT e.thread_id, e.id, e.api_call_id, e.event_type,
           CASE WHEN e.event_type IN ('tool_use_started', 'tool_use_complete',
                                      'tool_execution_completed', 'tool_execution_error')
                THEN json_extract(e.payload, '$.tool_name') END AS tool_name,
           CASE WHEN e.event_type IN ('tool_use_started', 'tool_use_complete',
                                      'tool_execution_completed', 'tool_execution_error')
                THEN json_extract(e.payload, '$.tool_call_id') END AS tool_call_id,
           CASE WHEN e.event_type = 'content_block'
                THEN json_extract(e.payload, '$.block_type')
                WHEN e.event_type = 'ide_context'
                THEN json_extract(e.payload, '$.context_type') END AS event_detail,
           e.payload, e.occurred_at, t.title, t.source, t.thread_type, t.updated_at
    FROM events e
    JOIN threads t ON t.id = e.thread_id
    WHERE t.archived = 0
      AND t.thread_type IN :thread_types
      AND (:through_event_id IS NULL OR e.id <= :through_event_id)
      AND e.event_type NOT IN ({','.join(repr(v) for v in _IGNORED_EVENT_TYPES)})
    ORDER BY e.thread_id, e.id
    """
).bindparams(bindparam("thread_types", expanding=True))


def _call_signature(payload: dict) -> str | None:
    """Return a stable equality fingerprint without retaining potentially sensitive args."""
    value = payload.get("input", payload.get("arguments", payload.get("tool_input")))
    if value is None:
        return None
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _error_category(payload: dict) -> str:
    """Coarsen provider-specific failure text into searchable diagnostic classes."""
    text_value = payload.get("error", payload.get("output", payload.get("content", "")))
    if not isinstance(text_value, str):
        try:
            text_value = json.dumps(text_value, ensure_ascii=False)
        except (TypeError, ValueError):
            text_value = str(text_value)
    value = text_value.casefold()
    categories = (
        ("cancelled", ("cancelled", "canceled", "interrupted")),
        ("rejected", ("doesn't want to proceed", "tool use was rejected", "declined")),
        ("timeout", ("timed out", "timeout")),
        ("permission", ("permission denied", "forbidden", "not permitted", "unauthorized")),
        ("not_found", ("not found", "no such file", "command not found", "exit code 127")),
        ("validation", ("inputvalidationerror", "invalid argument", "required parameter", "unexpected parameter")),
        ("precondition", ("has not been read yet", "modified since read", "read it first")),
        ("schema", ("does not match required schema", "schema validation")),
        ("unavailable", ("no such tool available", "is not connected", "is not enabled")),
        ("hook", ("hook error", "pretooluse:")),
        ("syntax", ("syntax error", "parse error", "unknown option")),
        ("conflict", ("conflict", "already exists")),
        ("execution", ("traceback (most recent call last)", "exit code 1", "eisdir:")),
    )
    return next((category for category, needles in categories if any(n in value for n in needles)), "other")


def _is_genuine_summary(payload: dict) -> bool:
    """Separate compacted conversation summaries from attachment/system machinery."""
    if payload.get("system_type") in {"compact_boundary", "compaction", "summary"}:
        return True
    if payload.get("summary_type") in {"compaction", "cursor_compaction"}:
        return True
    # Importers use a bare context_summary for actual compactions. Every known
    # attachment or system record has a system_type and must not become behavior.
    return payload.get("system_type") is None and bool(str(payload.get("content", "")).strip())


def _iter_traces(
    thread_types: tuple[str, ...], *, through_event_id: int | None = None,
) -> Iterator[_Trace]:
    """Stream projected traces from SQLite without retaining the corpus in memory."""
    with get_engine().connect().execution_options(stream_results=True) as conn:
        rows = conn.execute(_EVENT_QUERY, {
            "thread_types": thread_types,
            "through_event_id": through_event_id,
        })
        current_id: str | None = None
        title: str | None = None
        source: str | None = None
        thread_type = ""
        updated_at: str | None = None
        activities: list[_Activity] = []
        call_names: dict[str, str] = {}
        started_calls: set[str] = set()
        pending_calls: dict[str, _PendingCall] = {}
        recent_failures: dict[str, tuple[str | None, int]] = {}
        api_content_seen: set[str] = set()

        def append(shape: str, detail: str, event_id: int, occurred_at: str) -> None:
            activity = _Activity(shape, detail, event_id, occurred_at)
            # Several providers split one text/thinking block into adjacent twins.
            # Collapse only truly identical adjacent projections; repeated tool calls
            # remain separate because their event ids and call ids are distinct.
            if activities and activities[-1].shape == shape and activities[-1].detail == detail:
                if not shape.startswith("tool:"):
                    return
            activities.append(activity)

        for row in rows:
            tid = str(row[0])
            if current_id is not None and tid != current_id:
                yield _Trace(current_id, title, source, thread_type, updated_at, activities)
                activities = []
                call_names = {}
                started_calls = set()
                pending_calls = {}
                recent_failures = {}
                api_content_seen = set()
            if tid != current_id:
                current_id = tid
                title, source, thread_type = row[9], row[10], str(row[11])
                updated_at = str(row[12]) if row[12] is not None else None

            event_id, api_call_id, event_type = int(row[1]), row[2], str(row[3])
            tool_name, tool_call_id, event_detail = row[4], row[5], row[6]
            try:
                payload = json.loads(row[7]) if isinstance(row[7], str) else dict(row[7] or {})
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            occurred_at = str(row[8])

            if event_type in ("tool_use_started", "tool_use_complete"):
                name = str(tool_name or "unknown")
                if tool_call_id:
                    cid = str(tool_call_id)
                    call_names[cid] = name
                    if event_type == "tool_use_complete" and cid in started_calls:
                        previous = pending_calls.get(cid)
                        if previous and previous.signature is None:
                            pending_calls[cid] = _PendingCall(
                                previous.position, name, _call_signature(payload), previous.event_id,
                            )
                        continue  # the started event already represents this call
                    started_calls.add(cid)
                shape, detail = _tool_ids("call", name)
                append(shape, detail, event_id, occurred_at)
                if tool_call_id:
                    pending_calls[str(tool_call_id)] = _PendingCall(
                        len(activities) - 1, name, _call_signature(payload), event_id,
                    )
            elif event_type in ("tool_execution_completed", "tool_execution_error"):
                # Historical builders wrote the literal placeholder "unknown" on
                # most result events even though tool_call_id still pairs them to a
                # named call. Treat the placeholder as absent and recover that name.
                recorded_name = str(tool_name or "").strip()
                name = (
                    recorded_name
                    if recorded_name and recorded_name != "unknown"
                    else call_names.get(str(tool_call_id), "unknown")
                )
                pending = pending_calls.pop(str(tool_call_id), None) if tool_call_id else None
                signature = pending.signature if pending else None
                if event_type == "tool_execution_error":
                    kind = "error"
                    category = _error_category(payload)
                else:
                    category = None
                    failed = recent_failures.get(name)
                    # A same-tool success within the miner's default bounded-gap
                    # window is a recovery. Argument equality is compared by hash;
                    # raw arguments never enter the derived report.
                    if failed and (pending.position if pending else len(activities)) - failed[1] <= DEFAULT_MAX_GAP + 1:
                        if signature is None or failed[0] is None:
                            kind = "recovery"
                        elif signature == failed[0]:
                            kind = "recovery_same"
                        else:
                            kind = "recovery_changed"
                        recent_failures.pop(name, None)
                    else:
                        kind = "success"
                shape, detail = _tool_ids(kind, name, category)
                if pending is not None:
                    activities[pending.position] = _Activity(
                        shape, detail, pending.event_id, occurred_at,
                    )
                    position = pending.position
                else:
                    append(shape, detail, event_id, occurred_at)
                    position = len(activities) - 1
                if event_type == "tool_execution_error":
                    recent_failures[name] = (signature, position)
            elif event_type in ("text_complete", "thinking_complete"):
                kind = "assistant:text" if event_type == "text_complete" else "assistant:thinking"
                append(kind, kind, event_id, occurred_at)
                if api_call_id:
                    api_content_seen.add(str(api_call_id))
            elif event_type == "api_request_completed":
                # Live streaming capture has no granular twins; preserve the turn as
                # generic assistant output. File imports do have twins, so skip their
                # duplicate request summary.
                aid = str(api_call_id) if api_call_id else ""
                if not aid or aid not in api_content_seen:
                    append("assistant:response", "assistant:response", event_id, occurred_at)
                if aid:
                    api_content_seen.discard(aid)
            elif event_type in ("user_message_sent", "thread_message_sent"):
                append("user:message", "user:message", event_id, occurred_at)
            elif event_type == "context_summary":
                if _is_genuine_summary(payload):
                    append("context:summary", "context:summary", event_id, occurred_at)
            elif event_type == "model_change":
                append("model:change", "model:change", event_id, occurred_at)
            elif event_type == "hook_context":
                hook_name = str(payload.get("hook_name") or payload.get("name") or "unknown")
                hook_context = str(payload.get("context") or payload.get("content") or "")
                if not (hook_name == "response-check" and hook_context.strip() == "clean"):
                    append("context:hook", f"context:hook:{hook_name}", event_id, occurred_at)
            elif event_type == "queue_operation":
                append("user:queued", "user:queued", event_id, occurred_at)
            elif event_type == "ide_context":
                detail = f"context:ide:{event_detail}" if event_detail else "context:ide"
                append("context:ide", detail, event_id, occurred_at)
            elif event_type == "content_block":
                detail = f"content:block:{event_detail}" if event_detail else "content:block"
                append("content:block", detail, event_id, occurred_at)
            else:
                eid = f"event:{event_type}"
                append(eid, eid, event_id, occurred_at)

        if current_id is not None:
            yield _Trace(current_id, title, source, thread_type, updated_at, activities)


def _collapse(ids: Iterable[str], events: Iterable[int]) -> tuple[list[str], list[int]]:
    """Collapse adjacent identical symbols while preserving their first event anchor."""
    out_ids: list[str] = []
    out_events: list[int] = []
    for activity_id, event_id in zip(ids, events):
        if out_ids and out_ids[-1] == activity_id and not activity_id.startswith("tool:"):
            continue
        out_ids.append(activity_id)
        out_events.append(event_id)
    return out_ids, out_events


def _trace_patterns(
    ids: list[str], event_ids: list[int], *, max_length: int, max_gap: int,
) -> tuple[Counter[tuple[str, ...]], Counter[tuple[str, ...]], dict[tuple[str, ...], tuple[int, ...]]]:
    """Enumerate bounded-gap subsequences once for one trace.

    Occurrence counts include repeated appearances; support is added by the caller
    once per key in the returned counter.  A direct occurrence has no skipped event
    between any two pattern activities.
    """
    occurrences: Counter[tuple[str, ...]] = Counter()
    direct: Counter[tuple[str, ...]] = Counter()
    first: dict[tuple[str, ...], tuple[int, ...]] = {}
    n = len(ids)

    def extend(indexes: tuple[int, ...]) -> None:
        if len(indexes) >= 2:
            pattern = tuple(ids[i] for i in indexes)
            occurrences[pattern] += 1
            if all(b == a + 1 for a, b in zip(indexes, indexes[1:])):
                direct[pattern] += 1
            # Traces are chronological, so replacement retains the newest exact
            # occurrence for drill-down ordering and its deep-link anchor.
            first[pattern] = tuple(event_ids[i] for i in indexes)
        if len(indexes) >= max_length:
            return
        start = indexes[-1] + 1
        stop = min(n, indexes[-1] + max_gap + 2)
        for nxt in range(start, stop):
            extend((*indexes, nxt))

    for i in range(n):
        extend((i,))
    return occurrences, direct, first


def _pattern_id(abstraction: str, activities: tuple[str, ...]) -> str:
    blob = json.dumps([abstraction, activities], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _is_diagnostic(pattern: tuple[str, ...]) -> bool:
    return any(
        activity.startswith("tool:error") or activity.startswith("tool:recovery")
        for activity in pattern
    )


def _select_patterns(patterns: list[dict], limit: int) -> list[dict]:
    """Reserve catalog space for errors/recoveries before filling by score."""
    if limit <= 0:
        return []
    diagnostic = [p for p in patterns if _is_diagnostic(tuple(p["activities"]))]
    reserve = min(len(diagnostic), max(1, limit // 3))
    chosen = diagnostic[:reserve]
    chosen_ids = {p["id"] for p in chosen}
    chosen.extend(p for p in patterns if p["id"] not in chosen_ids)
    return chosen[:limit]


def mine_patterns(
    *,
    thread_types: tuple[str, ...] = DEFAULT_THREAD_TYPES,
    min_support: int = DEFAULT_MIN_SUPPORT,
    max_length: int = DEFAULT_MAX_LENGTH,
    max_gap: int = DEFAULT_MAX_GAP,
    max_patterns: int = DEFAULT_MAX_PATTERNS,
) -> dict:
    """Mine the complete open corpus and publish the pattern experiment artifacts.

    ``support`` is the number of distinct threads containing a pattern;
    ``occurrences`` includes repeats within a thread.  ``lift`` compares observed
    thread support with the independent support of the constituent activities.
    Ranking is lift-forward with logarithmic support. Diagnostic sequences get a
    modest boost, and catalog quotas keep errors/recoveries from being displaced by
    high-volume protocol shapes.
    """
    if not thread_types or any(not t.strip() for t in thread_types):
        raise ValueError("at least one non-empty thread type is required")
    if min_support < 2:
        raise ValueError("min_support must be at least 2")
    if not 2 <= max_length <= 5:
        raise ValueError("max_length must be between 2 and 5")
    if not 0 <= max_gap <= 8:
        raise ValueError("max_gap must be between 0 and 8")
    if max_patterns < 2:
        raise ValueError("max_patterns must be at least 2")

    paths = resolve_paths()
    engine = get_engine()
    with engine.connect() as conn:
        through_event_id = int(conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM events")).scalar() or 0)
        corpus_events = int(conn.execute(text("SELECT COUNT(*) FROM events WHERE id <= :upto"), {"upto": through_event_id}).scalar() or 0)

    support: dict[str, Counter[tuple[str, ...]]] = {
        "shape": Counter(), "detail": Counter(),
    }
    occurrences: dict[str, Counter[tuple[str, ...]]] = {
        "shape": Counter(), "detail": Counter(),
    }
    direct: dict[str, Counter[tuple[str, ...]]] = {
        "shape": Counter(), "detail": Counter(),
    }
    activity_support: dict[str, Counter[str]] = {"shape": Counter(), "detail": Counter()}
    activity_occurrences: dict[str, Counter[str]] = {"shape": Counter(), "detail": Counter()}
    trace_count = 0
    sequence_events = 0
    type_counts: Counter[str] = Counter()

    for trace in _iter_traces(
        tuple(dict.fromkeys(t.strip() for t in thread_types)),
        through_event_id=through_event_id,
    ):
        if not trace.activities:
            continue
        trace_count += 1
        type_counts[trace.thread_type] += 1
        sequence_events += len(trace.activities)
        event_ids = [a.event_id for a in trace.activities]
        for abstraction in ("shape", "detail"):
            ids, anchors = _collapse(
                (getattr(a, abstraction) for a in trace.activities), event_ids
            )
            activity_occurrences[abstraction].update(ids)
            activity_support[abstraction].update(set(ids))
            local_occ, local_direct, _ = _trace_patterns(
                ids, anchors, max_length=max_length, max_gap=max_gap
            )
            occurrences[abstraction].update(local_occ)
            direct[abstraction].update(local_direct)
            support[abstraction].update(local_occ.keys())

    patterns_by_abstraction: dict[str, list[dict]] = {"shape": [], "detail": []}
    for abstraction in ("shape", "detail"):
        for pattern, supp in support[abstraction].items():
            if supp < min_support or trace_count == 0:
                continue
            # Coarse call/success permutations are protocol volume, not behavior.
            # Concrete tool-success bursts remain available in the detail lens.
            if abstraction == "shape" and all(
                activity in {"tool:call", "tool:success"} for activity in pattern
            ):
                continue
            observed = supp / trace_count
            expected = math.prod(
                activity_support[abstraction][activity] / trace_count
                for activity in pattern
            )
            lift = observed / expected if expected else 0.0
            pmi = math.log2(lift) if lift > 0 else 0.0
            npmi = pmi / -math.log2(observed) if 0 < observed < 1 else 0.0
            # NPMI is bounded, so a rare provider envelope whose fields always
            # co-occur cannot win merely by producing an enormous raw lift.
            interestingness = math.log1p(supp) * max(0.0, npmi) * (
                1 + 0.15 * (len(pattern) - 2)
            )
            if _is_diagnostic(pattern):
                interestingness *= 1.75
            if len(set(pattern)) == 1:
                interestingness *= 0.45
            if abstraction == "detail" and all(
                activity.startswith(("tool:success:", "tool:call:")) for activity in pattern
            ):
                interestingness *= 0.35
            patterns_by_abstraction[abstraction].append({
                "id": _pattern_id(abstraction, pattern),
                "abstraction": abstraction,
                "activities": list(pattern),
                "length": len(pattern),
                "support": int(supp),
                "support_ratio": observed,
                "occurrences": int(occurrences[abstraction][pattern]),
                "direct_occurrences": int(direct[abstraction][pattern]),
                "lift": lift,
                "npmi": npmi,
                "interestingness": interestingness,
                "examples": [],
            })
        patterns_by_abstraction[abstraction].sort(
            key=lambda p: (p["interestingness"], p["support"], p["lift"]), reverse=True
        )

    # Keep both lenses represented even when coarse patterns have much larger support.
    shape_limit = max_patterns // 2
    detail_limit = max_patterns - shape_limit
    selected = (
        _select_patterns(patterns_by_abstraction["shape"], shape_limit)
        + _select_patterns(patterns_by_abstraction["detail"], detail_limit)
    )
    selected.sort(key=lambda p: (p["interestingness"], p["support"]), reverse=True)

    activity_ids = {activity for p in selected for activity in p["activities"]}
    vocabulary = {
        abstraction: {
            activity: _activity_meta(activity, abstraction)
            for activity in sorted(
                {a for p in selected if p["abstraction"] == abstraction for a in p["activities"]}
            )
        }
        for abstraction in ("shape", "detail")
    }
    # activity_ids is deliberately evaluated as a consistency assertion: every
    # selected id must appear in exactly one abstraction's vocabulary.
    assert len(activity_ids) <= sum(len(v) for v in vocabulary.values())

    generated_at = datetime.now(timezone.utc).isoformat()
    newest_examples, diagnostics = _write_match_index(
        paths.pattern_matches_path,
        selected,
        thread_types=tuple(dict.fromkeys(t.strip() for t in thread_types)),
        max_length=max_length,
        max_gap=max_gap,
        generated_at=generated_at,
        through_event_id=through_event_id,
    )
    for selected_pattern in selected:
        selected_pattern["examples"] = newest_examples.get(selected_pattern["id"], [])
        selected_pattern.update(diagnostics.get(selected_pattern["id"], {}))

    report = {
        "version": REPORT_VERSION,
        "experiment": "patterns",
        "status": "ready",
        "generated_at": generated_at,
        "through_event_id": through_event_id,
        "stale": False,
        "stale_events": 0,
        "report_path": str(paths.patterns_path),
        "config": {
            "thread_types": list(thread_types),
            "min_support": min_support,
            "max_length": max_length,
            "max_gap": max_gap,
            "max_patterns": max_patterns,
        },
        "corpus": {
            "threads": trace_count,
            "events": corpus_events,
            "sequence_events": sequence_events,
            "thread_types": dict(sorted(type_counts.items())),
        },
        "vocabulary": vocabulary,
        "patterns": selected,
    }
    _write_report(paths.patterns_path, report)
    _write_experiment_readme(paths.pattern_experiment_readme_path)
    return report


def _write_experiment_readme(path: Path) -> None:
    """Write the agent handoff beside the disposable experiment data."""
    content = """# Behavioral pattern mining experiment

This directory is disposable derived data. Regenerate it with `thread_archive patterns`.
It is not part of Archive's stable truth format or public MCP surface.

## Agent exploration

```sh
thread_archive patterns list --limit 20
thread_archive patterns list --query Bash --lens detail --sort lift
thread_archive patterns list --query recovery --lens detail
thread_archive patterns read PATTERN_ID --offset 0 --limit 50
```

All `list` and `read` output is JSON. `read` returns matching threads newest-first;
each row includes `thread_id`, `event_id`, and the exact `event_ids` sequence. Use
`thread_read(thread_id, around_event=event_id, mode='full')` to inspect the evidence.
Catalog rows also report source concentration and their first/latest matching dates,
making single-provider instrumentation motifs easy to distinguish from cross-source
behavior.

Tool calls are paired with their results before mining, so successful calls are one
activity rather than a protocol-level call/result pair. A same-tool success shortly
after a failure is labeled as a recovery, including whether its normalized arguments
changed. Error detail activities retain the tool and a coarse failure category.

## Artifacts

- `report.json`: compact catalog, metrics, vocabulary, and newest examples.
- `matches.db`: SQLite match index, one exact occurrence per supporting thread.

The local experimental HTTP surface mirrors these reads at
`/api/experiments/patterns/catalog` and
`/api/experiments/patterns/PATTERN_ID/matches`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _write_match_index(
    path: Path,
    patterns: list[dict],
    *,
    thread_types: tuple[str, ...],
    max_length: int,
    max_gap: int,
    generated_at: str,
    through_event_id: int,
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Publish one exact match per supporting thread for every visible pattern.

    The report stays small enough for the overview endpoint. This disposable SQLite
    companion carries the potentially millions of drill-down rows and provides a
    stable newest-first page order without rescanning the corpus on each click.
    """
    selected = {
        abstraction: {
            tuple(pattern["activities"]): pattern["id"]
            for pattern in patterns
            if pattern["abstraction"] == abstraction
        }
        for abstraction in ("shape", "detail")
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pattern-matches-", suffix=".db", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(tmp)
        conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE matches (
                pattern_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                title TEXT,
                source TEXT,
                event_id INTEGER NOT NULL,
                event_ids TEXT NOT NULL,
                matched_at TEXT NOT NULL,
                thread_updated_at TEXT,
                PRIMARY KEY (pattern_id, thread_id)
            );
            """
        )
        conn.execute("INSERT INTO metadata VALUES ('generated_at', ?)", (generated_at,))
        insert = (
            "INSERT INTO matches "
            "(pattern_id, thread_id, title, source, event_id, event_ids, matched_at, thread_updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        batch: list[tuple] = []
        for trace in _iter_traces(thread_types, through_event_id=through_event_id):
            if not trace.activities:
                continue
            event_ids = [activity.event_id for activity in trace.activities]
            occurred_at = {activity.event_id: activity.occurred_at for activity in trace.activities}
            for abstraction in ("shape", "detail"):
                if not selected[abstraction]:
                    continue
                ids, anchors = _collapse(
                    (getattr(activity, abstraction) for activity in trace.activities), event_ids
                )
                _, _, local_matches = _trace_patterns(
                    ids, anchors, max_length=max_length, max_gap=max_gap
                )
                for activities, pattern_id in selected[abstraction].items():
                    match = local_matches.get(activities)
                    if match is None:
                        continue
                    batch.append((
                        pattern_id,
                        trace.thread_id,
                        trace.title,
                        trace.source,
                        match[0],
                        json.dumps(match, separators=(",", ":")),
                        occurred_at[match[-1]],
                        trace.updated_at,
                    ))
            if len(batch) >= 5_000:
                conn.executemany(insert, batch)
                batch.clear()
        if batch:
            conn.executemany(insert, batch)
        conn.executescript(
            "CREATE INDEX matches_recent "
            "ON matches (pattern_id, matched_at DESC, event_id DESC);"
        )
        conn.commit()

        examples: dict[str, list[dict]] = {}
        diagnostics: dict[str, dict] = {}
        for pattern in patterns:
            rows = conn.execute(
                "SELECT thread_id, title, source, event_id, event_ids, matched_at "
                "FROM matches WHERE pattern_id = ? "
                "ORDER BY matched_at DESC, event_id DESC LIMIT ?",
                (pattern["id"], EXAMPLES_PER_PATTERN),
            ).fetchall()
            examples[pattern["id"]] = [
                {
                    "thread_id": row[0], "title": row[1], "source": row[2],
                    "event_id": row[3], "event_ids": json.loads(row[4]),
                    "matched_at": row[5],
                }
                for row in rows
            ]
            source_rows = conn.execute(
                "SELECT COALESCE(source, 'unknown'), COUNT(*) FROM matches "
                "WHERE pattern_id = ? GROUP BY COALESCE(source, 'unknown') "
                "ORDER BY COUNT(*) DESC, COALESCE(source, 'unknown') LIMIT 8",
                (pattern["id"],),
            ).fetchall()
            total = int(pattern["support"])
            dominant = int(source_rows[0][1]) if source_rows else 0
            dominant_ratio = dominant / total if total else 0.0
            date_row = conn.execute(
                "SELECT MIN(matched_at), MAX(matched_at), "
                "COUNT(DISTINCT substr(matched_at, 1, 7)) FROM matches WHERE pattern_id = ?",
                (pattern["id"],),
            ).fetchone()
            source_count = int(conn.execute(
                "SELECT COUNT(DISTINCT COALESCE(source, 'unknown')) FROM matches WHERE pattern_id = ?",
                (pattern["id"],),
            ).fetchone()[0])
            if source_count <= 1:
                concentration = "single-source"
            elif dominant_ratio >= 0.8:
                concentration = "source-skewed"
            else:
                concentration = "cross-source"
            diagnostics[pattern["id"]] = {
                "source_count": source_count,
                "dominant_source": source_rows[0][0] if source_rows else None,
                "dominant_source_ratio": dominant_ratio,
                "source_concentration": concentration,
                "sources": [
                    {"source": row[0], "threads": int(row[1]), "ratio": int(row[1]) / total}
                    for row in source_rows
                ] if total else [],
                "first_matched_at": date_row[0] if date_row else None,
                "last_matched_at": date_row[1] if date_row else None,
                "active_months": int(date_row[2] or 0) if date_row else 0,
            }
        conn.close()
        conn = None
        tmp.chmod(0o600)
        os.replace(tmp, path)
        return examples, diagnostics
    finally:
        if conn is not None:
            conn.close()
        tmp.unlink(missing_ok=True)


def read_pattern_matches(pattern_id: str, *, offset: int = 0, limit: int = 50) -> dict | None:
    """Return one newest-first page of threads matching a visible pattern."""
    report = read_patterns()
    pattern = next((item for item in report.get("patterns", []) if item.get("id") == pattern_id), None)
    if pattern is None:
        return None
    result = {
        "status": "ready",
        "generated_at": report.get("generated_at"),
        "pattern": pattern,
        "vocabulary": report.get("vocabulary", {}).get(pattern["abstraction"], {}),
        "total": int(pattern["support"]),
        "offset": offset,
        "limit": limit,
        "matches": [],
    }
    path = resolve_paths().pattern_matches_path
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        result["status"] = "not_indexed"
        return result
    try:
        metadata = conn.execute(
            "SELECT value FROM metadata WHERE key = 'generated_at'"
        ).fetchone()
        if metadata is None or metadata[0] != report.get("generated_at"):
            result["status"] = "not_indexed"
            return result
        rows = conn.execute(
            "SELECT thread_id, title, source, event_id, event_ids, matched_at, thread_updated_at "
            "FROM matches WHERE pattern_id = ? "
            "ORDER BY matched_at DESC, event_id DESC LIMIT ? OFFSET ?",
            (pattern_id, limit, offset),
        ).fetchall()
        result["matches"] = [
            {
                "thread_id": row[0], "title": row[1], "source": row[2],
                "event_id": row[3], "event_ids": json.loads(row[4]),
                "matched_at": row[5], "thread_updated_at": row[6],
            }
            for row in rows
        ]
        result["has_more"] = offset + len(rows) < result["total"]
        return result
    except (sqlite3.Error, json.JSONDecodeError):
        result["status"] = "not_indexed"
        result["matches"] = []
        return result
    finally:
        conn.close()


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".patterns-", suffix=".json", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def read_patterns() -> dict:
    """Read the last mined report and annotate it against the live index."""
    paths = resolve_paths()
    try:
        report = json.loads(paths.patterns_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "version": REPORT_VERSION,
            "status": "not_run",
            "report_path": str(paths.patterns_path),
            "stale": False,
            "stale_events": 0,
            "patterns": [],
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "version": REPORT_VERSION,
            "status": "invalid",
            "report_path": str(paths.patterns_path),
            "error": str(exc),
            "stale": False,
            "stale_events": 0,
            "patterns": [],
        }
    if not isinstance(report, dict) or report.get("version") != REPORT_VERSION:
        return {
            "version": REPORT_VERSION,
            "status": "invalid",
            "report_path": str(paths.patterns_path),
            "error": "unsupported pattern report format",
            "stale": False,
            "stale_events": 0,
            "patterns": [],
        }
    with get_engine().connect() as conn:
        current = int(conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM events")).scalar() or 0)
    through = int(report.get("through_event_id") or 0)
    report["stale_events"] = max(0, current - through)
    report["stale"] = current != through
    report["report_path"] = str(paths.patterns_path)
    return report


def search_patterns(
    *, query: str = "", lens: str = "all", sort: str = "interestingness",
    limit: int = 20,
) -> dict:
    """Return a compact, structured catalog for agent-side pattern exploration."""
    report = read_patterns()
    if report.get("status") != "ready":
        return {
            "status": report.get("status", "not_run"),
            "generated_at": report.get("generated_at"),
            "patterns": [],
            "next": "Run `thread_archive patterns` to publish the pattern catalog.",
        }
    if lens not in {"all", "shape", "detail"}:
        return {"status": "error", "error": "lens must be all, shape, or detail", "patterns": []}
    if sort not in {"interestingness", "support", "lift"}:
        return {"status": "error", "error": "sort must be interestingness, support, or lift", "patterns": []}
    terms = [term.casefold() for term in query.split() if term]
    vocabulary = report.get("vocabulary", {})

    def labels(pattern: dict) -> list[str]:
        vocab = vocabulary.get(pattern["abstraction"], {})
        return [vocab.get(activity, {}).get("label", activity) for activity in pattern["activities"]]

    candidates = []
    for pattern in report.get("patterns", []):
        if lens != "all" and pattern["abstraction"] != lens:
            continue
        activity_labels = labels(pattern)
        haystack = " ".join([pattern["id"], *pattern["activities"], *activity_labels]).casefold()
        if terms and not all(term in haystack for term in terms):
            continue
        candidates.append((pattern, activity_labels))
    candidates.sort(
        key=lambda item: (
            float(item[0].get(sort, 0)),
            int(item[0].get("support", 0)),
            float(item[0].get("lift", 0)),
        ),
        reverse=True,
    )
    items = [
        {
            "pattern_id": pattern["id"],
            "abstraction": pattern["abstraction"],
            "activities": activity_labels,
            "supporting_threads": pattern["support"],
            "support_ratio": pattern["support_ratio"],
            "occurrences": pattern["occurrences"],
            "lift": pattern["lift"],
            "interestingness": pattern["interestingness"],
            "source_count": pattern.get("source_count", 0),
            "dominant_source": pattern.get("dominant_source"),
            "dominant_source_ratio": pattern.get("dominant_source_ratio", 0),
            "source_concentration": pattern.get("source_concentration"),
            "first_matched_at": pattern.get("first_matched_at"),
            "last_matched_at": pattern.get("last_matched_at"),
        }
        for pattern, activity_labels in candidates[:limit]
    ]
    return {
        "status": "ready",
        "generated_at": report.get("generated_at"),
        "stale": report.get("stale", False),
        "stale_events": report.get("stale_events", 0),
        "query": query,
        "lens": lens,
        "sort": sort,
        "returned": len(items),
        "available": len(candidates),
        "patterns": items,
        "next": (
            "Run `thread_archive patterns read PATTERN_ID` to inspect matching "
            "threads and exact event anchors."
        ),
    }
