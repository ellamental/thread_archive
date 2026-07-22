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

This is intentionally an explicit batch operation.  Mining millions of events is
operator work, not latency a page request should spring on the watcher.  The CLI
writes ``patterns.json`` atomically under the archive home; the web endpoint reads
that derived report and reports how many events have landed since its watermark.
The report is disposable and is never part of the JSONL truth set.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import bindparam, text

from ._config import resolve_paths
from ._store import get_engine

REPORT_VERSION = 1
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
    "tool_execution_started",
    "archived_duplicate",
)


@dataclass(frozen=True)
class _Activity:
    """One projected event, carrying both mining alphabets and its source anchor."""

    shape: str
    detail: str
    event_id: int


@dataclass
class _Trace:
    thread_id: str
    title: str | None
    source: str | None
    thread_type: str
    activities: list[_Activity]


def _display_event(event_type: str) -> str:
    return event_type.replace("_", " ")


def _tool_ids(kind: str, name: str | None) -> tuple[str, str]:
    clean = (name or "unknown").strip()[:200] or "unknown"
    return f"tool:{kind}", f"tool:{kind}:{clean}"


def _activity_meta(activity_id: str, abstraction: str) -> dict:
    """Turn a compact stable id into viewer-ready vocabulary metadata."""
    parts = activity_id.split(":", 2)
    if parts[0] == "tool":
        kind = parts[1]
        tool = parts[2] if len(parts) > 2 else None
        verb = {"call": "call", "success": "success", "error": "error"}.get(kind, kind)
        return {
            "id": activity_id,
            "kind": f"tool_{kind}",
            "detail": tool,
            "label": f"{verb} · {tool}" if tool else f"tool {verb}",
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
           t.title, t.source, t.thread_type
    FROM events e
    JOIN threads t ON t.id = e.thread_id
    WHERE t.archived = 0
      AND t.thread_type IN :thread_types
      AND e.event_type NOT IN ({','.join(repr(v) for v in _IGNORED_EVENT_TYPES)})
    ORDER BY e.thread_id, e.id
    """
).bindparams(bindparam("thread_types", expanding=True))


def _iter_traces(thread_types: tuple[str, ...]) -> Iterator[_Trace]:
    """Stream projected traces from SQLite without retaining the corpus in memory."""
    with get_engine().connect().execution_options(stream_results=True) as conn:
        rows = conn.execute(_EVENT_QUERY, {"thread_types": thread_types})
        current_id: str | None = None
        title: str | None = None
        source: str | None = None
        thread_type = ""
        activities: list[_Activity] = []
        call_names: dict[str, str] = {}
        started_calls: set[str] = set()
        api_content_seen: set[str] = set()

        def append(shape: str, detail: str, event_id: int) -> None:
            activity = _Activity(shape, detail, event_id)
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
                yield _Trace(current_id, title, source, thread_type, activities)
                activities = []
                call_names = {}
                started_calls = set()
                api_content_seen = set()
            if tid != current_id:
                current_id = tid
                title, source, thread_type = row[7], row[8], str(row[9])

            event_id, api_call_id, event_type = int(row[1]), row[2], str(row[3])
            tool_name, tool_call_id, event_detail = row[4], row[5], row[6]

            if event_type in ("tool_use_started", "tool_use_complete"):
                name = str(tool_name or "unknown")
                if tool_call_id:
                    cid = str(tool_call_id)
                    call_names[cid] = name
                    if event_type == "tool_use_complete" and cid in started_calls:
                        continue  # the started event already represents this call
                    started_calls.add(cid)
                shape, detail = _tool_ids("call", name)
                append(shape, detail, event_id)
            elif event_type in ("tool_execution_completed", "tool_execution_error"):
                name = str(tool_name or call_names.get(str(tool_call_id), "unknown"))
                kind = "error" if event_type == "tool_execution_error" else "success"
                shape, detail = _tool_ids(kind, name)
                append(shape, detail, event_id)
            elif event_type in ("text_complete", "thinking_complete"):
                kind = "assistant:text" if event_type == "text_complete" else "assistant:thinking"
                append(kind, kind, event_id)
                if api_call_id:
                    api_content_seen.add(str(api_call_id))
            elif event_type == "api_request_completed":
                # Live streaming capture has no granular twins; preserve the turn as
                # generic assistant output. File imports do have twins, so skip their
                # duplicate request summary.
                aid = str(api_call_id) if api_call_id else ""
                if not aid or aid not in api_content_seen:
                    append("assistant:response", "assistant:response", event_id)
                if aid:
                    api_content_seen.discard(aid)
            elif event_type in ("user_message_sent", "thread_message_sent"):
                append("user:message", "user:message", event_id)
            elif event_type == "context_summary":
                append("context:summary", "context:summary", event_id)
            elif event_type == "model_change":
                append("model:change", "model:change", event_id)
            elif event_type == "hook_context":
                append("context:hook", "context:hook", event_id)
            elif event_type == "queue_operation":
                append("user:queued", "user:queued", event_id)
            elif event_type == "ide_context":
                detail = f"context:ide:{event_detail}" if event_detail else "context:ide"
                append("context:ide", detail, event_id)
            elif event_type == "content_block":
                detail = f"content:block:{event_detail}" if event_detail else "content:block"
                append("content:block", detail, event_id)
            else:
                eid = f"event:{event_type}"
                append(eid, eid, event_id)

        if current_id is not None:
            yield _Trace(current_id, title, source, thread_type, activities)


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
            first.setdefault(pattern, tuple(event_ids[i] for i in indexes))
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


def mine_patterns(
    *,
    thread_types: tuple[str, ...] = DEFAULT_THREAD_TYPES,
    min_support: int = DEFAULT_MIN_SUPPORT,
    max_length: int = DEFAULT_MAX_LENGTH,
    max_gap: int = DEFAULT_MAX_GAP,
    max_patterns: int = DEFAULT_MAX_PATTERNS,
) -> dict:
    """Mine the complete open corpus and atomically publish ``patterns.json``.

    ``support`` is the number of distinct threads containing a pattern;
    ``occurrences`` includes repeats within a thread.  ``lift`` compares observed
    thread support with the independent support of the constituent activities.
    The ranking blends support with normalized positive pointwise mutual
    information, preventing both ubiquitous boilerplate and one-off curiosities
    from monopolizing the report.
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
    examples: dict[str, dict[tuple[str, ...], list[dict]]] = {
        "shape": defaultdict(list), "detail": defaultdict(list),
    }
    trace_count = 0
    sequence_events = 0
    type_counts: Counter[str] = Counter()

    for trace in _iter_traces(tuple(dict.fromkeys(t.strip() for t in thread_types))):
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
            local_occ, local_direct, local_first = _trace_patterns(
                ids, anchors, max_length=max_length, max_gap=max_gap
            )
            occurrences[abstraction].update(local_occ)
            direct[abstraction].update(local_direct)
            support[abstraction].update(local_occ.keys())
            for pattern in local_occ:
                if support[abstraction][pattern] < min_support:
                    continue
                dest = examples[abstraction][pattern]
                if len(dest) >= EXAMPLES_PER_PATTERN:
                    continue
                anchors_for_pattern = local_first[pattern]
                dest.append({
                    "thread_id": trace.thread_id,
                    "title": trace.title,
                    "source": trace.source,
                    "event_id": anchors_for_pattern[0],
                    "event_ids": list(anchors_for_pattern),
                })

    patterns_by_abstraction: dict[str, list[dict]] = {"shape": [], "detail": []}
    for abstraction in ("shape", "detail"):
        for pattern, supp in support[abstraction].items():
            if supp < min_support or trace_count == 0:
                continue
            observed = supp / trace_count
            expected = math.prod(
                activity_support[abstraction][activity] / trace_count
                for activity in pattern
            )
            lift = observed / expected if expected else 0.0
            pmi = math.log2(lift) if lift > 0 else 0.0
            npmi = pmi / -math.log2(observed) if 0 < observed < 1 else 0.0
            interestingness = supp * max(0.0, npmi) * (1 + 0.15 * (len(pattern) - 2))
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
                "interestingness": interestingness,
                "examples": examples[abstraction].get(pattern, []),
            })
        patterns_by_abstraction[abstraction].sort(
            key=lambda p: (p["interestingness"], p["support"], p["lift"]), reverse=True
        )

    # Keep both lenses represented even when coarse patterns have much larger support.
    shape_limit = max_patterns // 2
    detail_limit = max_patterns - shape_limit
    selected = (
        patterns_by_abstraction["shape"][:shape_limit]
        + patterns_by_abstraction["detail"][:detail_limit]
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

    report = {
        "version": REPORT_VERSION,
        "status": "ready",
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
    return report


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
