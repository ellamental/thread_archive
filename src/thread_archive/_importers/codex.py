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
from ._line_stream import line_stream_importer

logger = logging.getLogger(__name__)


def _codex_import_lines(sess, thread_id, all_lines, new_lines, _meta):
    call_names, call_inputs = _codex_call_maps(all_lines)
    prior_lines = all_lines[: len(all_lines) - len(new_lines)]
    messages = _build_codex_messages(
        new_lines,
        _codex_model(prior_lines),
        call_names,
        call_inputs,
        ambient=_codex_ambient_annotations(prior_lines),
    )
    return assemble_events(sess, thread_id, messages, DefaultEventBuilder())




def _codex_session_meta(lines: list[dict]) -> dict[str, Any]:
    for line in lines:
        if line.get("type") != "session_meta":
            continue
        payload = line.get("payload")
        return payload if isinstance(payload, dict) else {}
    return {}


def _codex_source_metadata(meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Thread-level source_metadata from ``session_meta``: cwd, CLI version, and
    git provenance (commit/branch/repo the session ran against)."""
    out: dict[str, Any] = {}
    if meta.get("cwd"):
        out["cwd"] = meta["cwd"]
    if meta.get("cli_version"):
        out["cli_version"] = meta["cli_version"]
    git = meta.get("git")
    if isinstance(git, dict):
        git_out = {k: git[k] for k in ("commit_hash", "branch", "repository_url") if git.get(k)}
        if git_out:
            out["git"] = git_out
    return out or None


def _codex_context_annotations(line: dict) -> dict[str, str]:
    """The effort/personality a ``turn_context`` line names, ``{}`` when none.

    Like the model (see :func:`codex_line_model`), these are declared per turn and
    hold until re-declared, so they're tracked as ambient state over the stream."""
    if line.get("type") != "turn_context":
        return {}
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("effort", "personality"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def _codex_ambient_annotations(prior_lines: list[dict]) -> dict[str, str]:
    """The effort/personality in effect entering a chunk (incremental imports
    resume mid-session, so the turn_context that named them can sit behind the
    watermark)."""
    state: dict[str, str] = {}
    for line in prior_lines:
        if isinstance(line, dict):
            state.update(_codex_context_annotations(line))
    return state


def codex_line_model(line: dict) -> Optional[str]:
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
        named = codex_line_model(line)
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


_CODEX_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)


def _codex_image_block(url: str, ts: Optional[str]) -> dict[str, Any]:
    """An image content block from a codex ``images`` entry (a ``data:`` URL,
    decoded into the builder's base64 source shape) — or a pointer block keeping
    the raw URL when it isn't one, so the reference survives either way."""
    m = _CODEX_DATA_URL_RE.match(url)
    if m:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)},
            "start_timestamp": ts,
        }
    return {"type": "image", "url": url, "start_timestamp": ts}


def _codex_user_message(payload: dict[str, Any], ts: Optional[str]) -> Optional[dict[str, Any]]:
    message = str(payload.get("message") or "")
    images = payload.get("images")
    blocks = [
        _codex_image_block(url, ts)
        for url in (images if isinstance(images, list) else [])
        if isinstance(url, str) and url
    ]
    if not message and not blocks:
        return None
    annotations: dict[str, Any] = {}
    local_images = payload.get("local_images")
    if isinstance(local_images, list) and local_images:
        annotations["local_images"] = local_images
    text_elements = payload.get("text_elements")
    if isinstance(text_elements, list) and text_elements:
        annotations["text_elements"] = text_elements
    provider_data: dict[str, Any] = {"provider": "codex"}
    if annotations:
        provider_data["annotations"] = annotations
    return {
        "role": "user",
        "created_at": ts,
        "content_text": message,
        "content_blocks": blocks,
        "provider_message_id": payload.get("turn_id") or ts or "",
        "provider_data": provider_data,
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


_CODEX_USAGE_FIELDS = (
    # source key in last_token_usage → canonical usage key
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("reasoning_output_tokens", "thinking_tokens"),
    ("cached_input_tokens", "cache_read_tokens"),
)


def _codex_accumulate_usage(msg: dict[str, Any], payload: dict[str, Any]) -> None:
    """Fold a ``token_count`` line's ``info.last_token_usage`` into the assistant
    turn's ``provider_data["usage"]``.

    Each token_count measures the one API request it follows; an assembled
    assistant turn spans every request between two user messages, so the
    per-request counts are summed — the turn's usage is the total it billed.
    ``total_tokens`` is derivable and omitted. ``model_context_window`` rides as
    an annotation (data about the turn, not tokens it consumed)."""
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    last = info.get("last_token_usage")
    if isinstance(last, dict):
        usage = msg["provider_data"].setdefault("usage", {})
        for src, dst in _CODEX_USAGE_FIELDS:
            value = last.get(src)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[dst] = usage.get(dst, 0) + value
    window = info.get("model_context_window")
    if isinstance(window, int) and not isinstance(window, bool):
        msg["provider_data"].setdefault("annotations", {})["model_context_window"] = window


def _build_codex_messages(
    lines: list[dict],
    model: str,
    call_names: dict[str, str],
    call_inputs: dict[str, dict[str, Any]],
    ambient: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Assemble codex's interleaved line stream into canonical NormalizedMessages.

    ``model`` is the model entering the chunk; the lines themselves re-declare it
    per turn (see :func:`codex_line_model`), so it is tracked as the stream is
    walked and each assistant turn carries the model that actually served it.
    ``ambient`` is the effort/personality entering the chunk, tracked the same
    way (turn_context re-declares them per turn) and carried onto each assistant
    turn's annotations.
    """
    messages: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    ambient = dict(ambient or {})

    def flush() -> None:
        nonlocal cur
        if cur is not None and cur["content_blocks"]:
            messages.append(cur)
        cur = None

    def assistant(ts: Optional[str]) -> dict[str, Any]:
        nonlocal cur
        if cur is None:
            provider_data: dict[str, Any] = {"provider": "codex", "model": model}
            if ambient:
                provider_data["annotations"] = dict(ambient)
            cur = {
                "role": "assistant",
                "created_at": ts,
                "content_text": "",
                "content_blocks": [],
                "provider_message_id": ts or "",
                "provider_data": provider_data,
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

        named = codex_line_model(line)
        if named and named != model:
            model = named
            if cur is not None:
                # The turn's own context line lands mid-message (codex opens a turn
                # with task_started, then turn_context), so correct the turn already
                # in flight rather than only the ones after it.
                cur["provider_data"]["model"] = model

        named_ann = _codex_context_annotations(line)
        if named_ann:
            ambient.update(named_ann)
            if cur is not None:
                # Same mid-message correction as the model above.
                cur["provider_data"].setdefault("annotations", {}).update(named_ann)

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
            if line_type == "event_msg" and payload_type == "token_count":
                # The raw line is preserved as a codex_token_count block above;
                # this additionally maps it to structured usage on the turn.
                _codex_accumulate_usage(assistant(ts), payload)

    flush()
    return messages


#: Import one Codex session JSONL into the archive's event log.
#:
#: Codex writes a different JSONL shape from Claude Code: ``event_msg.user_message``
#: / ``event_msg.agent_message`` are the canonical transcript, and
#: ``response_item.function_call*`` / ``custom_tool_call*`` are tool events.
import_codex_session_incremental = line_stream_importer(
    "codex",
    prepare=lambda all_lines, _path, _source_id: _codex_session_meta(all_lines),
    has_importable_content=_codex_has_importable_content,
    make_title=lambda all_lines, _meta: _codex_title(all_lines),
    make_source_metadata=_codex_source_metadata,
    import_lines=_codex_import_lines,
    not_found_msg="Codex session file not found",
)
