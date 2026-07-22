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
    occurred_at: str


@dataclass
class _Trace:
    thread_id: str
    title: str | None
    source: str | None
    thread_type: str
    updated_at: str | None
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
           e.occurred_at, t.title, t.source, t.thread_type, t.updated_at
    FROM events e
    JOIN threads t ON t.id = e.thread_id
    WHERE t.archived = 0
      AND t.thread_type IN :thread_types
      AND (:through_event_id IS NULL OR e.id <= :through_event_id)
      AND e.event_type NOT IN ({','.join(repr(v) for v in _IGNORED_EVENT_TYPES)})
    ORDER BY e.thread_id, e.id
    """
).bindparams(bindparam("thread_types", expanding=True))


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
                api_content_seen = set()
            if tid != current_id:
                current_id = tid
                title, source, thread_type = row[8], row[9], str(row[10])
                updated_at = str(row[11]) if row[11] is not None else None

            event_id, api_call_id, event_type = int(row[1]), row[2], str(row[3])
            tool_name, tool_call_id, event_detail = row[4], row[5], row[6]
            occurred_at = str(row[7])

            if event_type in ("tool_use_started", "tool_use_complete"):
                name = str(tool_name or "unknown")
                if tool_call_id:
                    cid = str(tool_call_id)
                    call_names[cid] = name
                    if event_type == "tool_use_complete" and cid in started_calls:
                        continue  # the started event already represents this call
                    started_calls.add(cid)
                shape, detail = _tool_ids("call", name)
                append(shape, detail, event_id, occurred_at)
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
                kind = "error" if event_type == "tool_execution_error" else "success"
                shape, detail = _tool_ids(kind, name)
                append(shape, detail, event_id, occurred_at)
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
                append("context:summary", "context:summary", event_id, occurred_at)
            elif event_type == "model_change":
                append("model:change", "model:change", event_id, occurred_at)
            elif event_type == "hook_context":
                append("context:hook", "context:hook", event_id, occurred_at)
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
                "examples": [],
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

    generated_at = datetime.now(timezone.utc).isoformat()
    newest_examples = _write_match_index(
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
thread_archive patterns read PATTERN_ID --offset 0 --limit 50
```

All `list` and `read` output is JSON. `read` returns matching threads newest-first;
each row includes `thread_id`, `event_id`, and the exact `event_ids` sequence. Use
`thread_read(thread_id, around_event=event_id, mode='full')` to inspect the evidence.

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
) -> dict[str, list[dict]]:
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
        conn.close()
        conn = None
        tmp.chmod(0o600)
        os.replace(tmp, path)
        return examples
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
