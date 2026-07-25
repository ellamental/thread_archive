"""Searchable-content extraction: an event's payload → FTS shadow rows.

Reads the payload dict keys directly — the importer's ``DefaultEventBuilder``
produces exactly these payload shapes, so there's no dependency on a dataclass
event-type hierarchy.

Each extractor returns ``(content, content_type, tool_name)`` tuples; an event
with no searchable content returns ``[]``.

Not every preserved event earns an index row: see :data:`UNINDEXED_CONTENT_TYPES`
for the content the archive keeps in truth but deliberately leaves unsearchable.
"""

from __future__ import annotations

import json
from typing import Optional

# Event types that carry searchable content (must match the dispatch below).
#
# NB: assistant text reaches the archive two ways, and exactly one of them may be
# indexed per api_call. File importers (DefaultEventBuilder) emit granular
# text_complete / thinking_complete twins beside an api_request_completed summary
# that duplicates them; a source captured live as it streams
# emits token text_delta events — never indexed — and the assembled turn exists
# only in the summary's content_blocks. So the granular twins are indexed
# unconditionally, and api_request_completed is indexed ONLY for api_calls with
# no twin — a gate the callers apply (see fts.index_events / rebuild_fts), since
# it needs context beyond one event. Indexing both would double-count; indexing
# neither loses every live-captured assistant turn.
INDEXABLE_EVENT_TYPES = [
    "user_message_sent",
    "text_complete",
    "thinking_complete",
    "api_request_completed",  # twin-gated by the callers — see NB above
    "tool_use_started",
    "tool_use_complete",
    "tool_execution_completed",
    "context_summary",
    "tool_execution_error",
    "thread_message_sent",
    "ide_context",
    "content_block",
    "message",
]


#: Content types extracted from truth but deliberately kept OUT of the search
#: index — preserved and readable, never searchable by content.
#:
#: Tool output is what a tool handed back: file bodies, command stdout, API
#: responses, stack traces. It is the bulk of the corpus by bytes (measured: 1.72
#: of 2.06 GB indexed, 36% of documents) and the worst of it by signal — a grep
#: dump or a re-read file puts thousands of incidental term occurrences behind
#: whichever conversation happened to run the command, so a query matches the
#: *machine's* words rather than anyone's. Ranking measured better without it on
#: both eval protocols, not merely cheaper: the tail it removes was competing with
#: real answers.
#:
#: The tool *call* stays indexed. It is small (0.15 GB), it carries the tool name
#: and its arguments, and "when did we run this" is a question the archive is
#: expected to answer — excluding calls too measured worse on every metric.
#:
#: Truth is untouched: the JSONL keeps every byte, ``thread_read`` replays these
#: events in full, and the code axis reads ``event_paths``, not this index. What
#: changes is only what a content search can match.
UNINDEXED_CONTENT_TYPES = frozenset({"tool_result", "tool_error"})


def _to_str(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _block_search_text(block) -> str:
    """Best-effort human-readable text from an arbitrary content block, skipping
    binary/base64 payloads (the ``source`` blob on image/document blocks). Used to
    make preserved-but-unmodeled blocks (``content_block``) searchable without
    indexing megabytes of base64."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return _to_str(block)
    parts: list[str] = []
    for key, value in block.items():
        if key in ("type", "source"):  # ``source`` carries base64 image/doc data
            continue
        if isinstance(value, str):
            if len(value) > 1000 and " " not in value[:100]:
                continue  # looks like an opaque/base64 blob
            parts.append(value)
        elif isinstance(value, (dict, list)):
            parts.append(_to_str(value))
    return " ".join(parts).strip()


def _strip_frontmatter(text: str) -> str:
    """Strip YAML frontmatter (---\\n...\\n---) from markdown content."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip()
    return text


# Input keys whose string values are indexed whole as body text (frontmatter-
# stripped, up to the overall 2000-char document cap). Every other key is
# indexed as ``key=value`` metadata with the value capped, so a Write's
# file_path or a Grep's pattern stays searchable beside the body.
_BODY_KEYS = frozenset(
    ("content", "text", "body", "message", "query", "prompt", "command", "description", "entry")
)


def _fts_tool_use(payload: dict) -> list[tuple[str, str, Optional[str]]]:
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("input")
    if tool_name.startswith("mcp__") and isinstance(tool_input, dict) and "command" in tool_input:
        # Heredoc bodies span many lines; index the first line, truncated.
        tool_name = tool_input["command"].split("\n", 1)[0][:200]

    content_parts = [payload.get("tool_name") or ""]
    if isinstance(tool_input, dict):
        body_parts: list[str] = []
        meta_parts: list[str] = []
        for key, val in tool_input.items():
            if key in _BODY_KEYS and isinstance(val, str) and val:
                body_parts.append(_strip_frontmatter(val))
            else:
                meta_parts.append(f"{key}={_to_str(val)[:500]}")
        # Metadata leads so the 2000-char cap can't truncate a small key
        # (file_path, pattern) away behind a large body (a Write, a heredoc).
        content_parts.extend(meta_parts)
        content_parts.extend(body_parts)
    elif tool_input is not None:
        content_parts.append(str(tool_input))

    content = " ".join(content_parts).strip()
    return [(content[:2000], "tool", tool_name or None)] if content else []


def _fts_tool_completed(payload: dict) -> list[tuple[str, str, Optional[str]]]:
    output = payload.get("output")
    tool_name = payload.get("tool_name")
    if payload.get("is_error"):
        error_text = output or payload.get("error", "")
        return [(_to_str(error_text), "tool_error", tool_name)] if error_text else []
    if output:
        return [(_to_str(output), "tool_result", tool_name)]
    return []


def extract_fts_content(event_type: str, payload: dict) -> list[tuple[str, str, Optional[str]]]:
    """Extract searchable ``(content, content_type, tool_name)`` tuples from an event.

    The index-policy seam: :data:`UNINDEXED_CONTENT_TYPES` is filtered out here,
    after extraction, so every writer and every checker agrees on what an event
    *should* have in the index without restating the rule. The incremental write
    path, the bulk rebuild, and ``verify``'s truth-vs-index reconciliation all read
    this one function — a policy applied in any of them alone would make the others
    report drift.
    """
    return [t for t in _extract_fts_content(event_type, payload)
            if t[1] not in UNINDEXED_CONTENT_TYPES]


def _extract_fts_content(event_type: str, payload: dict) -> list[tuple[str, str, Optional[str]]]:
    """Per-event-type extraction, before the index policy applies."""
    if not payload:
        return []

    if event_type == "user_message_sent":
        content = payload.get("content", "")
        if not content:
            return []
        if content.lstrip().startswith("This session is being continued from a previous conversation"):
            return [(content, "continuation_summary", None)]
        return [(content, "user", None)]

    if event_type == "api_request_completed":
        results: list[tuple[str, str, Optional[str]]] = []
        for block in payload.get("content_blocks", []) or []:
            bt = block.get("type")
            if bt == "thinking" and block.get("thinking"):
                results.append((block["thinking"], "thinking", None))
            elif bt == "text" and block.get("text"):
                results.append((block["text"], "text", None))
        return results

    if event_type == "text_complete":
        text = payload.get("text", "")
        return [(text, "text", None)] if text else []

    if event_type == "thinking_complete":
        text = payload.get("text", "")
        return [(text, "thinking", None)] if text else []

    if event_type in ("tool_use_started", "tool_use_complete"):
        return _fts_tool_use(payload)

    if event_type == "tool_execution_completed":
        return _fts_tool_completed(payload)

    if event_type == "tool_execution_error":
        error = payload.get("error", "")
        return [(_to_str(error), "tool_error", payload.get("tool_name"))] if error else []

    if event_type == "context_summary":
        content = payload.get("content", "")
        return [(content[:2000], "context_summary", None)] if content else []

    if event_type == "thread_message_sent":
        content = payload.get("content", "")
        return [(content, "user", None)] if content else []

    if event_type == "ide_context":
        # Index the opened-file path / selection body so "what was I looking at"
        # is searchable. file_path (when present) leads so a path query matches.
        content = payload.get("content", "")
        file_path = payload.get("file_path")
        text = f"{file_path}\n{content}" if file_path else content
        return [(text[:2000], "ide_context", None)] if text.strip() else []

    if event_type == "content_block":
        # An unmodeled block preserved verbatim; index its human-readable text.
        text = _block_search_text(payload.get("data"))
        content_type = payload.get("block_type") or "content_block"
        return [(text[:2000], content_type, None)] if text.strip() else []

    if event_type == "message":
        # A preserved non-standard-role turn. Prefer its text; fall back to blocks.
        content = payload.get("content", "")
        content_type = payload.get("role") or "message"
        if content.strip():
            return [(content, content_type, None)]
        blocks = payload.get("content_blocks") or []
        text = " ".join(t for t in (_block_search_text(b) for b in blocks) if t)
        return [(text[:2000], content_type, None)] if text.strip() else []

    return []
