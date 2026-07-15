"""Codex (`~/.codex`) incremental session import + line-stream assembler.

The ``_codex_*`` / ``_build_codex_messages`` helpers are pure functions over the
line dicts; the orchestration is wired onto the shared scaffold + ``assemble_events``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from thread_archive._thread_import import DefaultEventBuilder

from ._events import assemble_events
from ._line_stream import import_line_stream_session
from ._result import IncrementalImportResult

logger = logging.getLogger(__name__)


def import_codex_session_incremental(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
    """Import a Codex session JSONL into the archive's event log.

    Codex writes a different JSONL shape from Claude Code: ``event_msg.user_message``
    / ``event_msg.agent_message`` are the canonical transcript, and
    ``response_item.function_call*`` / ``custom_tool_call*`` are tool events.
    """

    def _do_import(sess, thread_id, all_lines, new_lines, _meta):
        call_names, call_inputs = _codex_call_maps(all_lines)
        prior_lines = all_lines[: len(all_lines) - len(new_lines)]
        messages = _build_codex_messages(
            new_lines, _codex_model(prior_lines), call_names, call_inputs
        )
        return assemble_events(sess, thread_id, messages, DefaultEventBuilder())

    return import_line_stream_session(
        source="codex",
        source_id=source_id,
        session_path=session_path,
        session=session,
        not_found_msg=f"Codex session file not found: {session_path}",
        prepare=lambda all_lines, _path: _codex_session_meta(all_lines),
        has_importable_content=_codex_has_importable_content,
        make_title=lambda all_lines, _meta: _codex_title(all_lines),
        make_source_metadata=lambda meta: {"cwd": meta["cwd"]} if meta.get("cwd") else None,
        import_lines=_do_import,
    )


def _codex_session_meta(lines: list[dict]) -> dict[str, Any]:
    for line in lines:
        if line.get("type") != "session_meta":
            continue
        payload = line.get("payload")
        return payload if isinstance(payload, dict) else {}
    return {}


def _codex_line_model(line: dict) -> Optional[str]:
    """The model a codex line names, or None when it names none.

    Codex declares the serving model per *turn*, not once per session: each turn
    opens with a ``turn_context`` (``payload.model``), and a mid-session switch
    emits an ``event_msg``/``thread_settings_applied``
    (``payload.thread_settings.model``). ``session_meta`` names a model only on
    older CLIs, so it is a fallback, not the source — reading it alone leaves every
    turn attributed to a bare ``"codex"``.
    """
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return None
    line_type = line.get("type")
    if line_type == "turn_context":
        named = payload.get("model")
    elif line_type == "event_msg" and payload.get("type") == "thread_settings_applied":
        settings = payload.get("thread_settings")
        named = settings.get("model") if isinstance(settings, dict) else None
    elif line_type == "session_meta":
        named = payload.get("model")
    else:
        return None
    return named.strip() if isinstance(named, str) and named.strip() else None


def _codex_model(prior_lines: list[dict]) -> str:
    """The model in effect entering a chunk: the last one named before it.

    An incremental import resumes mid-session, so the turn that named the model can
    sit behind the watermark — hence the scan back over the lines already imported.
    """
    for line in reversed(prior_lines):
        if not isinstance(line, dict):
            continue
        named = _codex_line_model(line)
        if named:
            return named
    return "codex"


def _codex_has_importable_content(lines: list[dict]) -> bool:
    for line in lines:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        if line.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            if payload.get("message"):
                return True
    return False


def _codex_first_user_line(lines: list[dict]) -> Optional[str]:
    """First non-empty line of the first ``event_msg``/``user_message``, or None."""
    for line in lines:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        if line.get("type") == "event_msg" and payload.get("type") == "user_message":
            msg = str(payload.get("message") or "").strip()
            if msg:
                return next((part.strip() for part in msg.splitlines() if part.strip()), "")
    return None


def _codex_title(lines: list[dict]) -> str:
    meta = _codex_session_meta(lines)
    thread_name = meta.get("thread_name")
    if isinstance(thread_name, str) and thread_name.strip():
        return thread_name.strip()[:100]
    first = _codex_first_user_line(lines)
    if first is not None:
        return (first[:97] + "...") if len(first) > 100 else first
    return "Codex Session"


def _codex_call_input(payload: dict[str, Any], payload_type: str) -> Optional[dict[str, Any]]:
    """Extract a tool call's input dict, or None when there's nothing to store."""
    if payload_type == "custom_tool_call":
        raw_input = payload.get("input")
        if isinstance(raw_input, dict):
            return raw_input
        if raw_input is not None:
            return {"input": str(raw_input)}
        return None

    raw_args = payload.get("arguments")
    if isinstance(raw_args, str) and raw_args:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {"arguments": raw_args}
        return parsed if isinstance(parsed, dict) else None
    if isinstance(raw_args, dict):
        return raw_args
    return None


def _codex_call_maps(lines: list[dict]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    names: dict[str, str] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for line in lines:
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        if line.get("type") != "response_item":
            continue
        payload_type = payload.get("type")
        if payload_type not in {"function_call", "custom_tool_call"}:
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        name = payload.get("name")
        if isinstance(name, str) and name:
            names[call_id] = name
        call_input = _codex_call_input(payload, payload_type)
        if call_input is not None:
            inputs[call_id] = call_input
    return names, inputs


def _codex_reasoning_text(payload: dict) -> str:
    parts: list[str] = []
    for key in ("summary", "content"):
        v = payload.get(key)
        if isinstance(v, list):
            for b in v:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b, str):
                    parts.append(b)
        elif isinstance(v, str):
            parts.append(v)
    return "\n".join(t for t in parts if t).strip()


def _codex_user_message(payload: dict[str, Any], ts: Optional[str]) -> Optional[dict[str, Any]]:
    message = str(payload.get("message") or "")
    if not message:
        return None
    return {
        "role": "user",
        "created_at": ts,
        "content_text": message,
        "content_blocks": [],
        "provider_message_id": payload.get("turn_id") or ts or "",
        "provider_data": {"provider": "codex"},
    }


def _codex_tool_use_block(
    payload: dict[str, Any], ts: Optional[str],
    call_names: dict[str, str], call_inputs: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return {
        "type": "tool_use",
        "id": call_id,
        "name": call_names.get(call_id, payload.get("name") or "unknown"),
        "input": call_inputs.get(call_id, {}),
        "start_timestamp": ts,
    }


_CODEX_EXIT_LINE_RE = re.compile(r"(?:Process exited with code|Exit code:)\s*(-?\d+)\s*$")


def _codex_output_is_error(output: str) -> bool:
    """True only on an unambiguous failure signal in a codex tool output.

    ``function_call_output`` / ``custom_tool_call_output`` payloads carry no
    structured error flag; the exit status lives in the output text's wrapper
    header — a ``Process exited with code N`` / ``Exit code: N`` line within the
    first few lines — or, for JSON-shaped outputs, an integer ``exit_code``
    (top-level or under ``metadata``). Only the header is checked so an output
    merely *quoting* an exit-code line deep in its body can't mark the call
    failed; anything ambiguous stays a success."""
    for line in output.splitlines()[:6]:
        m = _CODEX_EXIT_LINE_RE.match(line.strip())
        if m:
            return int(m.group(1)) != 0
    if output.startswith("{"):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return False
        if isinstance(parsed, dict):
            exit_code = parsed.get("exit_code")
            if exit_code is None and isinstance(parsed.get("metadata"), dict):
                exit_code = parsed["metadata"].get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                return exit_code != 0
    return False


def _codex_tool_result_block(
    payload: dict[str, Any], ts: Optional[str], call_names: dict[str, str],
) -> Optional[dict[str, Any]]:
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = payload.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, default=str)
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "name": call_names.get(call_id, "unknown"),
        "content": output,
        "is_error": _codex_output_is_error(output),
        "start_timestamp": ts,
    }


def _codex_preserved_block(
    line_type: Any, payload_type: Any, payload: dict[str, Any], ts: Optional[str],
) -> dict[str, Any]:
    """Wrap a codex kind the importer doesn't model as a content block that survives
    import — Archivist, not Filter.

    A ``message`` response_item, web-search results, images, an unmodeled
    ``event_msg`` (token_count, task_started/complete, reasoning deltas, errors), or a
    future line/payload kind would otherwise be dropped into oblivion. Keep the raw
    payload plus its type. The block's ``type`` is namespaced (``codex_<kind>``) so it
    isn't one of the builder's modeled block types, which makes the builder preserve
    it verbatim as a ``content_block`` event. The builder stores the whole block
    under the event's ``data`` key (a dedup-content key), so the raw payload is
    covered by the dedup hash and re-imports stay idempotent."""
    kind = payload_type or line_type or "unknown"
    return {
        "type": f"codex_{kind}",
        "codex_type": payload_type,
        "codex_line_type": line_type,
        "raw": payload,
        "start_timestamp": ts,
    }


def _codex_assistant_block(
    line_type: Any,
    payload_type: Any,
    payload: dict[str, Any],
    ts: Optional[str],
    call_names: dict[str, str],
    call_inputs: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if line_type == "event_msg":
        if payload_type == "agent_message":
            message = str(payload.get("message") or "")
            return {"type": "text", "text": message, "start_timestamp": ts} if message else None
        # user_message is consumed upstream; any other event_msg kind is preserved
        # rather than dropped.
        return _codex_preserved_block(line_type, payload_type, payload, ts)

    if line_type == "response_item":
        if payload_type == "reasoning":
            text = _codex_reasoning_text(payload)
            return {"type": "thinking", "text": text, "start_timestamp": ts} if text else None
        if payload_type in ("function_call", "custom_tool_call"):
            return _codex_tool_use_block(payload, ts, call_names, call_inputs)
        if payload_type in ("function_call_output", "custom_tool_call_output"):
            return _codex_tool_result_block(payload, ts, call_names)
        # A `message` response_item, web-search results, images, or any future kind.
        return _codex_preserved_block(line_type, payload_type, payload, ts)

    # `session_meta` is consumed as thread metadata (cwd/title) upstream; any OTHER
    # line type (turn_context, compacted, a future kind) is preserved so no provider
    # record is silently dropped on import.
    if line_type == "session_meta":
        return None
    return _codex_preserved_block(line_type, payload_type, payload, ts)


def _build_codex_messages(
    lines: list[dict],
    model: str,
    call_names: dict[str, str],
    call_inputs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble codex's interleaved line stream into canonical NormalizedMessages.

    ``model`` is the model entering the chunk; the lines themselves re-declare it
    per turn (see :func:`_codex_line_model`), so it is tracked as the stream is
    walked and each assistant turn carries the model that actually served it.
    """
    messages: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur["content_blocks"]:
            messages.append(cur)
        cur = None

    def assistant(ts: Optional[str]) -> dict[str, Any]:
        nonlocal cur
        if cur is None:
            cur = {
                "role": "assistant",
                "created_at": ts,
                "content_text": "",
                "content_blocks": [],
                "provider_message_id": ts or "",
                "provider_data": {"provider": "codex", "model": model},
            }
        return cur

    for line in lines:
        if not isinstance(line, dict):
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        line_type = line.get("type")
        payload_type = payload.get("type")
        ts = line.get("timestamp")

        named = _codex_line_model(line)
        if named and named != model:
            model = named
            if cur is not None:
                # The turn's own context line lands mid-message (codex opens a turn
                # with task_started, then turn_context), so correct the turn already
                # in flight rather than only the ones after it.
                cur["provider_data"]["model"] = model

        if line_type == "event_msg" and payload_type == "user_message":
            user_msg = _codex_user_message(payload, ts)
            if user_msg is None:
                continue
            flush()
            messages.append(user_msg)
            continue

        block = _codex_assistant_block(
            line_type, payload_type, payload, ts, call_names, call_inputs
        )
        if block is not None:
            assistant(ts)["content_blocks"].append(block)

    flush()
    return messages
