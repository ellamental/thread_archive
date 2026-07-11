"""Grok CLI (`~/.grok/sessions`) incremental session import + assembler.

``chat_history.jsonl`` is the content spine; real timestamps come from sibling
files (``updates.jsonl`` for tool times, ``prompt_history.jsonl`` for user-prompt
times, ``summary.json`` for session metadata). Pure ``_grok_*`` helpers; orchestration
wired onto the shared scaffold.

We seed the timestamp anchor from the session ``created_at`` and rely on dedup_key
for re-imports. Refining it to seed from the newest persisted event (so a final
answer landing in a later pass than its tool rounds doesn't sort to the top) is
deferred.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder

from ._events import assemble_events
from ._line_stream import import_line_stream_session
from ._result import IncrementalImportResult

logger = logging.getLogger(__name__)

_GROK_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)


def import_grok_session_incremental(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
    """Import a Grok CLI session (``chat_history.jsonl`` path) into the event log."""
    session_path = Path(session_path)

    def _do_import(sess, thread_id, all_lines, new_lines, meta):
        session_dir = session_path.parent
        tool_times = _grok_tool_timestamps(session_dir)
        prompt_times = _grok_prompt_ts_map(session_dir, source_id)
        base_ts = _parse_grok_timestamp(meta.get("created_at")) or datetime.now(timezone.utc)
        messages = _build_grok_messages(
            new_lines, meta, tool_times, prompt_times,
            prefix_lines=all_lines[: len(all_lines) - len(new_lines)],
        )
        return assemble_events(sess, thread_id, messages, DefaultEventBuilder(), base_prev_ts=base_ts)

    return import_line_stream_session(
        source="grok",
        source_id=source_id,
        session_path=session_path,
        session=session,
        not_found_msg=f"Grok session file not found: {session_path}",
        prepare=lambda _all_lines, path: _grok_session_meta(path.parent),
        has_importable_content=_grok_has_importable_content,
        make_title=lambda all_lines, meta: _grok_title(all_lines, meta),
        make_source_metadata=lambda meta: _grok_source_metadata(meta, source_id),
        import_lines=_do_import,
    )


def _parse_grok_timestamp(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except (ValueError, TypeError):
        return None


def _grok_unix_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(v, str):
        return _parse_grok_timestamp(v)
    return None


def _grok_session_meta(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "summary.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _grok_meta_info(meta: dict[str, Any]) -> dict[str, Any]:
    info = meta.get("info")
    return info if isinstance(info, dict) else {}


def _grok_model(meta: dict[str, Any]) -> str:
    model = meta.get("current_model_id")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return "grok"


def _grok_source_metadata(meta: dict[str, Any], source_id: str) -> dict[str, Any]:
    info = _grok_meta_info(meta)
    data: dict[str, Any] = {
        "provider": "grok",
        "session_id": info.get("id") or source_id,
        "cwd": info.get("cwd") or meta.get("git_root_dir"),
        "model": meta.get("current_model_id"),
        "agent_name": meta.get("agent_name"),
        "git_branch": meta.get("head_branch"),
        "git_commit": meta.get("head_commit"),
        "git_remotes": meta.get("git_remotes"),
        "created_at": meta.get("created_at"),
    }
    return {k: v for k, v in data.items() if v is not None}


def _grok_provider_data(meta: dict[str, Any], role: str) -> dict[str, Any]:
    info = _grok_meta_info(meta)
    data: dict[str, Any] = {
        "provider": "grok",
        "role": role,
        "session_id": info.get("id"),
        "cwd": info.get("cwd"),
        "model": meta.get("current_model_id"),
        "agent_name": meta.get("agent_name"),
    }
    return {k: v for k, v in data.items() if v is not None}


def _grok_user_text(line: dict) -> Optional[str]:
    content = line.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts) if parts else None
    return None


def _grok_extract_query(text_content: str) -> str:
    match = _GROK_USER_QUERY_RE.search(text_content)
    if match:
        return match.group(1).strip()
    stripped = text_content.strip()
    if stripped.startswith(("<user_info>", "<system-reminder>", "<environment")):
        return ""
    return stripped


def _grok_reasoning_text(line: dict) -> str:
    summary = line.get("summary")
    if isinstance(summary, list):
        parts = [b.get("text", "") for b in summary if isinstance(b, dict) and b.get("type") == "summary_text"]
        return "\n\n".join(p for p in parts if p)
    return ""


def _grok_tool_input(tool_call: dict) -> dict[str, Any]:
    raw = tool_call.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}
    return {}


def _grok_first_query(all_lines: list[dict]) -> Optional[str]:
    for line in all_lines:
        if line.get("type") != "user" or line.get("synthetic_reason"):
            continue
        text_content = _grok_user_text(line)
        if text_content is None:
            continue
        query = _grok_extract_query(text_content)
        if query.strip():
            return query
    return None


def _grok_title(all_lines: list[dict], meta: dict[str, Any]) -> str:
    for key in ("generated_title", "session_summary"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:100]
    query = _grok_first_query(all_lines)
    if query:
        first = next((part.strip() for part in query.splitlines() if part.strip()), "")
        return (first[:97] + "...") if len(first) > 100 else first
    return "Grok Session"


def _grok_has_importable_content(lines: list[dict]) -> bool:
    for line in lines:
        line_type = line.get("type")
        if line_type == "assistant":
            content = line.get("content")
            if (isinstance(content, str) and content.strip()) or line.get("tool_calls"):
                return True
        elif line_type == "tool_result":
            return True
        elif line_type == "reasoning":
            if _grok_reasoning_text(line):
                return True
        elif line_type == "user" and not line.get("synthetic_reason"):
            text_content = _grok_user_text(line)
            if text_content and _grok_extract_query(text_content).strip():
                return True
    return False


def _grok_apply_tool_update(update: dict[str, Any], ts: Optional[datetime], rec: dict[str, Any]) -> None:
    status = update.get("status")
    if status:
        rec["status"] = status
        if ts:
            rec["end"] = ts
    elif ts and "start" not in rec:
        rec["start"] = ts


def _grok_fold_tool_update(obj: dict[str, Any], out: dict[str, dict[str, Any]]) -> None:
    update = (obj.get("params") or {}).get("update") or {}
    session_update = update.get("sessionUpdate")
    if session_update not in ("tool_call", "tool_call_update"):
        return
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    ts = _grok_unix_ts(obj.get("timestamp"))
    rec = out.setdefault(call_id, {})
    if session_update == "tool_call":
        if ts and "start" not in rec:
            rec["start"] = ts
        return
    _grok_apply_tool_update(update, ts, rec)


def _grok_tool_timestamps(session_dir: Path) -> dict[str, dict[str, Any]]:
    path = session_dir / "updates.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return out
    with handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            _grok_fold_tool_update(obj, out)
    return out


def _grok_prompt_ts_map(session_dir: Path, source_id: str) -> dict[str, list[datetime]]:
    path = session_dir.parent / "prompt_history.jsonl"
    out: dict[str, list[datetime]] = {}
    if not path.exists():
        return out
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return out
    with handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if obj.get("session_id") != source_id:
                continue
            ts = _parse_grok_timestamp(obj.get("timestamp"))
            prompt = obj.get("prompt")
            if ts is None or not isinstance(prompt, str):
                continue
            out.setdefault(prompt.strip(), []).append(ts)
    return out


def _grok_pop_prompt_ts(prompt_times: dict[str, list[datetime]], query: str) -> Optional[datetime]:
    key = query.strip()
    queue = prompt_times.get(key)
    if queue:
        return queue.pop(0)
    head = key[:80]
    for stored_key, queue in prompt_times.items():
        if queue and (stored_key.startswith(head) or key.startswith(stored_key[:80])):
            return queue.pop(0)
    return None


def _grok_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _grok_build_user_message(
    text: str, query: str, prompt_times: dict[str, list[datetime]], meta: dict[str, Any]
) -> dict[str, Any]:
    """Build a user NormalizedMessage carrying the FULL user-line text.

    ``text`` is the complete user text (harness-injected ``<user_info>`` /
    ``<environment>`` context and all); ``query`` is the ``<user_query>`` span (or the
    same text when the line is unwrapped), used only to match the prompt-history
    timestamp and noted under ``user_query`` so the span stays recoverable. Nothing
    outside the query tags is discarded — the archive captures the whole turn."""
    provider_data = _grok_provider_data(meta, "user")
    query = query.strip()
    if query and query != text.strip():
        provider_data["user_query"] = query
    return {
        "role": "user",
        # Keep the prompt-time lookup keyed on the query span (unchanged behavior);
        # fall back to the full text only for context-only turns that have no span.
        "created_at": _grok_iso(_grok_pop_prompt_ts(prompt_times, query or text)),
        "content_text": text,
        "content_blocks": [],
        "provider_message_id": "",
        "provider_data": provider_data,
    }


def _grok_preserve_line(
    line: dict[str, Any],
    meta: dict[str, Any],
    *,
    block_type: str,
    extra_provider_data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Preserve a grok line the modeled path would otherwise silently drop.

    Emitted as a ``role="system"`` NormalizedMessage carrying the line's readable
    text plus a single content block, so the shared event builder turns it into a
    ``context_summary`` event (given a block, a system message is never dropped) and
    keeps the raw line — plus any extra tags — under ``provider_data``. This upholds
    the archive's capture-EVERYTHING invariant: no provider record is skipped on
    import. The text is never truncated. ``created_at`` is left unset so the builder
    inherits the prior turn's time (monotonic, flagged) rather than fabricating one."""
    text = _grok_user_text(line) or ""
    provider_data = {
        **_grok_provider_data(meta, "system"),
        "grok_line_type": line.get("type"),
        "raw_line": line,
    }
    if extra_provider_data:
        provider_data.update(extra_provider_data)
    return {
        "role": "system",
        "created_at": None,
        "content_text": text,
        "content_blocks": [{"type": block_type, "text": text}],
        "provider_message_id": "",
        "provider_data": provider_data,
    }


def _grok_build_assistant_message(
    line: dict[str, Any],
    pending_thinking: Optional[str],
    tool_times: dict[str, dict[str, Any]],
    tool_names: dict[str, str],
    model_default: str,
    meta: dict[str, Any],
) -> Optional[dict[str, Any]]:
    content = line.get("content")
    content = content if isinstance(content, str) else (str(content) if content else "")
    tool_calls = line.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    if not content.strip() and not tool_calls and not pending_thinking:
        return None

    model = line.get("model_id") or model_default
    start_ts: Optional[datetime] = None
    for tool_call in tool_calls:
        cid = tool_call.get("id")
        started = tool_times.get(cid, {}).get("start") if isinstance(cid, str) else None
        if started and (start_ts is None or started < start_ts):
            start_ts = started

    blocks: list[dict[str, Any]] = []
    if pending_thinking:
        blocks.append({"type": "thinking", "text": pending_thinking})
    if content.strip():
        blocks.append({"type": "text", "text": content})
    for tool_call in tool_calls:
        cid = tool_call.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        name = tool_call.get("name") or "unknown"
        tool_names[cid] = name
        blocks.append({
            "type": "tool_use", "id": cid, "name": name,
            "input": _grok_tool_input(tool_call),
            "start_timestamp": _grok_iso(tool_times.get(cid, {}).get("start")),
        })
    return {
        "role": "assistant",
        "created_at": _grok_iso(start_ts),
        "content_text": "",
        "content_blocks": blocks,
        "provider_message_id": "",
        "provider_data": {**_grok_provider_data(meta, "assistant"), "model": model},
    }


def _grok_tool_result_block(
    line: dict, tool_times: dict[str, dict[str, Any]], tool_names: dict[str, str]
) -> Optional[dict[str, Any]]:
    cid = line.get("tool_call_id")
    if not isinstance(cid, str) or not cid:
        return None
    output = line.get("content")
    if not isinstance(output, str):
        output = json.dumps(output, default=str) if output is not None else ""
    info = tool_times.get(cid, {})
    return {
        "type": "tool_result", "tool_use_id": cid,
        "name": tool_names.get(cid, "unknown"),
        "content": output,
        "is_error": info.get("status") == "failed",
        "start_timestamp": _grok_iso(info.get("end")),
    }


def _grok_fold_tool_result(
    line: dict,
    cur: Optional[dict[str, Any]],
    tool_times: dict[str, dict[str, Any]],
    tool_names: dict[str, str],
    model_default: str,
    meta: dict[str, Any],
) -> Optional[dict[str, Any]]:
    block = _grok_tool_result_block(line, tool_times, tool_names)
    if block is None:
        return cur
    if cur is None:
        cur = {
            "role": "assistant",
            "created_at": block["start_timestamp"],
            "content_text": "",
            "content_blocks": [],
            "provider_message_id": "",
            "provider_data": {**_grok_provider_data(meta, "assistant"), "model": model_default},
        }
    cur["content_blocks"].append(block)
    return cur


def _grok_accumulate_thinking(pending_thinking: Optional[str], line: dict) -> Optional[str]:
    thought = _grok_reasoning_text(line)
    if not thought:
        return pending_thinking
    return pending_thinking + "\n\n" + thought if pending_thinking else thought


def _grok_user_turn(
    line: dict, prompt_times: dict[str, list[datetime]], meta: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Build the normalized message for a genuine (non-synthetic) user line.

    Synthetic/injected turns are preserved-and-tagged by the caller. Here we keep the
    FULL text — context-only turns (a line that is only ``<user_info>`` /
    ``<system-reminder>`` / ``<environment>`` with no ``<user_query>``) are preserved
    rather than dropped, and the ``<user_query>`` span keeps the text
    around it."""
    text_content = _grok_user_text(line)
    if text_content is None or not text_content.strip():
        return None
    query = _grok_extract_query(text_content)
    return _grok_build_user_message(text_content, query, prompt_times, meta)


def _harvest_tool_names(lines: list[dict]) -> dict[str, str]:
    """tool_call id→name from every assistant line in ``lines``. The already-
    imported prefix of an incremental batch must contribute its names, or a
    ``tool_result`` landing in a later poll than its ``tool_calls`` line
    resolves to ``"unknown"`` — permanently, since the payload is baked into
    the event log."""
    names: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, dict) or line.get("type") != "assistant":
            continue
        tool_calls = line.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            cid = tool_call.get("id")
            if isinstance(cid, str) and cid:
                names[cid] = tool_call.get("name") or "unknown"
    return names


def _build_grok_messages(
    new_lines: list[dict],
    meta: dict[str, Any],
    tool_times: dict[str, dict[str, Any]],
    prompt_times: dict[str, list[datetime]],
    prefix_lines: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Assemble grok's interleaved line stream into canonical NormalizedMessages.

    ``prefix_lines`` is the file's already-imported prefix: it seeds the
    tool-name map so results split across increments still resolve."""
    messages: list[dict[str, Any]] = []
    model_default = _grok_model(meta)
    tool_names: dict[str, str] = _harvest_tool_names(prefix_lines or [])
    pending_thinking: Optional[str] = None
    cur: Optional[dict[str, Any]] = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur["content_blocks"]:
            messages.append(cur)
        cur = None

    for line in new_lines:
        if not isinstance(line, dict):
            continue
        line_type = line.get("type")

        if line_type == "system":
            # Grok system lines/prompts are real records — preserve them as a tagged
            # system event instead of dropping. Flush the in-progress assistant turn
            # first so ordering holds, but don't reset pending thinking: a system
            # line isn't a turn boundary and mustn't discard dangling reasoning.
            flush()
            messages.append(_grok_preserve_line(line, meta, block_type="grok_system"))
            continue

        if line_type == "user":
            if line.get("synthetic_reason"):
                # Synthetic/injected user turn — keep AND tag (with the reason + raw
                # line) rather than drop. Not a real turn boundary, so leave the
                # in-progress assistant/thinking state alone.
                flush()
                messages.append(_grok_preserve_line(
                    line, meta, block_type="grok_synthetic_user",
                    extra_provider_data={
                        "synthetic": True,
                        "synthetic_reason": line.get("synthetic_reason"),
                        "grok_original_role": "user",
                    },
                ))
                continue
            user_msg = _grok_user_turn(line, prompt_times, meta)
            if user_msg is None:
                continue
            flush()
            pending_thinking = None
            messages.append(user_msg)
            continue

        if line_type == "reasoning":
            pending_thinking = _grok_accumulate_thinking(pending_thinking, line)
            continue

        if line_type == "assistant":
            built = _grok_build_assistant_message(
                line, pending_thinking, tool_times, tool_names, model_default, meta
            )
            if built is None:
                continue
            flush()
            cur = built
            pending_thinking = None
            continue

        if line_type == "tool_result":
            cur = _grok_fold_tool_result(line, cur, tool_times, tool_names, model_default, meta)
            continue

        # Any other grok line type (outside {system,user,reasoning,assistant,
        # tool_result}) — a new or unmodeled type must never vanish on import.
        # Preserve the raw line as a tagged event instead of silently ignoring it.
        flush()
        messages.append(
            _grok_preserve_line(line, meta, block_type=f"grok_{line_type or 'unknown'}")
        )

    flush()
    return messages
