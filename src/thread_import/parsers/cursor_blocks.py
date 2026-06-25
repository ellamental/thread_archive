"""Pure content-block builders for the Cursor parser.

These are the stateless helper bodies extracted verbatim from
``CursorParser`` — every Cursor content shape (pre-parsed ``content_blocks``,
the legacy ``thinking``/``tool_calls``/``tool_results`` fields, and the v2
export's ``code_blocks``/``tool_call``/``thinking``) plus the fallback and
tool-input normalization. ``CursorParser`` delegates to these so the class keeps
its method surface while the heavy bodies live here.

Block construction uses the ``ProviderParser`` static factory methods
(``create_text_block`` etc.), so the emitted dict/list shapes and the running
``seq`` numbering are byte-identical to the original in-class form.
"""

import json
from typing import Any, Dict, List, cast

from ..tool_names import FILE_TOOLS, PROVIDER_PATH_FIELDS, normalize_tool_name
from .base import ContentBlock, ProviderParser


def build_content_blocks(
    msg: Dict[str, Any],
    raw_blocks: List[Dict],
    fallback_content: Any,
) -> List[ContentBlock]:
    """Build normalized content blocks from Cursor message data.

    Each Cursor content shape (pre-parsed raw_blocks, legacy
    thinking/tool_calls/tool_results, the v2 export's code_blocks/tool_call/
    thinking) is appended in turn by a dedicated helper, all threading the
    same running ``seq`` counter so block ordering and seq numbering are
    unchanged from the single-function form.
    """
    blocks: List[ContentBlock] = []
    seq = 0

    seq = append_raw_blocks(blocks, raw_blocks, seq)
    seq = append_legacy_thinking(blocks, msg, seq)
    seq = append_legacy_tool_calls(blocks, msg, seq)
    seq = append_legacy_tool_results(blocks, msg, seq)
    seq = append_v2_code_blocks(blocks, msg, seq)
    seq = append_v2_tool_call(blocks, msg, seq)
    seq = append_v2_thinking(blocks, msg, seq)

    # If no blocks yet, create from fallback content
    if not blocks and fallback_content:
        append_fallback(blocks, fallback_content, seq)

    return blocks


def append_raw_blocks(
    blocks: List[ContentBlock], raw_blocks: List[Dict], seq: int
) -> int:
    """Append blocks from Cursor's pre-parsed ``content_blocks`` array.

    Each ``raw_block`` is dispatched by its ``type`` to a dedicated appender
    that returns the number of blocks it added; ``seq`` advances by that
    count, preserving the original ordering and seq numbering.
    """
    appenders = {
        "text": append_raw_text,
        "tool_use": append_raw_tool_use,
        "tool_result": append_raw_tool_result,
        "thinking": append_raw_thinking,
        "code": append_raw_code,
    }
    for raw_block in raw_blocks or []:
        block_type = raw_block.get("type", "text")
        appender = appenders.get(block_type, append_raw_unknown)
        seq += appender(blocks, raw_block, seq)
    return seq


def append_raw_text(blocks: List[ContentBlock], raw_block: Dict, seq: int) -> int:
    text = raw_block.get("text", "")
    if text:
        blocks.append(ProviderParser.create_text_block(text, seq))
        return 1
    return 0


def append_raw_tool_use(blocks: List[ContentBlock], raw_block: Dict, seq: int) -> int:
    original_name = raw_block.get("name", "unknown")
    canonical_name = normalize_tool_name(original_name)
    input_data = raw_block.get("input", {})
    block = ProviderParser.create_tool_use_block(canonical_name, input_data, seq)
    if canonical_name != original_name:
        block["provider_tool_name"] = original_name
    blocks.append(block)
    return 1


def append_raw_tool_result(
    blocks: List[ContentBlock], raw_block: Dict, seq: int
) -> int:
    name = raw_block.get("name", "unknown")
    content = raw_block.get("content")
    is_error = raw_block.get("is_error", False)
    blocks.append(ProviderParser.create_tool_result_block(name, content, seq, is_error))
    return 1


def append_raw_thinking(blocks: List[ContentBlock], raw_block: Dict, seq: int) -> int:
    text = raw_block.get("text", "")
    if text:
        blocks.append(ProviderParser.create_thinking_block(text, seq))
        return 1
    return 0


def append_raw_code(blocks: List[ContentBlock], raw_block: Dict, seq: int) -> int:
    code = raw_block.get("code", "")
    language = raw_block.get("language")
    blocks.append({
        "type": "code",
        "code": code,
        "language": language,
        "seq": seq,
    })
    return 1


def append_raw_unknown(blocks: List[ContentBlock], raw_block: Dict, seq: int) -> int:
    """Unknown type - try to extract text."""
    text = raw_block.get("text") or raw_block.get("content", "")
    if text and isinstance(text, str):
        blocks.append(ProviderParser.create_text_block(text, seq))
        return 1
    return 0


def append_legacy_thinking(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append thinking blocks from the legacy ``thinking``/``reasoning`` fields."""
    thinking = msg.get("thinking") or msg.get("reasoning")
    if thinking:
        if isinstance(thinking, str):
            blocks.append(ProviderParser.create_thinking_block(thinking, seq))
            seq += 1
        elif isinstance(thinking, list):
            for t in thinking:
                if isinstance(t, str):
                    blocks.append(ProviderParser.create_thinking_block(t, seq))
                    seq += 1
                elif isinstance(t, dict):
                    text = t.get("text", "")
                    if text:
                        blocks.append(ProviderParser.create_thinking_block(text, seq))
                        seq += 1
    return seq


def append_legacy_tool_calls(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append tool_use blocks from the legacy ``tool_calls``/``toolCalls`` array."""
    tool_calls = msg.get("tool_calls") or msg.get("toolCalls", [])
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                original_name = tc.get("name") or tc.get("function", {}).get("name", "unknown")
                canonical_name = normalize_tool_name(original_name)
                input_data = tc.get("arguments") or tc.get("input") or tc.get("function", {}).get("arguments", {})
                # Parse JSON string arguments if needed
                if isinstance(input_data, str):
                    try:
                        input_data = json.loads(input_data)
                    except (json.JSONDecodeError, TypeError):
                        pass
                input_data = normalize_tool_input(original_name, canonical_name, input_data, msg)
                block = ProviderParser.create_tool_use_block(canonical_name, input_data, seq)
                if canonical_name != original_name:
                    block["provider_tool_name"] = original_name
                blocks.append(block)
                seq += 1
    return seq


def append_legacy_tool_results(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append tool_result blocks from the legacy ``tool_results``/``toolResults`` array."""
    tool_results = msg.get("tool_results") or msg.get("toolResults", [])
    if tool_results:
        for tr in tool_results:
            if isinstance(tr, dict):
                name = tr.get("name", "unknown")
                content = tr.get("content") or tr.get("result")
                is_error = tr.get("is_error", False) or tr.get("isError", False)
                blocks.append(ProviderParser.create_tool_result_block(name, content, seq, is_error))
                seq += 1
    return seq


def append_v2_code_blocks(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append code blocks from the v2 export ``code_blocks`` array (cursor_export.py output)."""
    code_blocks = msg.get("code_blocks", [])
    if code_blocks:
        for cb in code_blocks:
            if isinstance(cb, dict):
                # v2 format: {uri, version, codeBlockIdx, content, languageId}
                code = cb.get("content", "")
                language = cb.get("languageId") or cb.get("language")
                uri = cb.get("uri", {})
                file_path = uri.get("path") if isinstance(uri, dict) else None
                blocks.append(cast(ContentBlock, {
                    "type": "code",
                    "code": code,
                    "language": language,
                    "file_path": file_path,
                    "seq": seq,
                }))
                seq += 1
    return seq


def append_v2_tool_call(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append tool_use (+ optional tool_result) from the v2 ``tool_call`` object.

    This contains the structured tool call data from toolFormerData.
    """
    tool_call = msg.get("tool_call")
    if tool_call and isinstance(tool_call, dict):
        original_name = tool_call.get("name", "unknown")
        canonical_name = normalize_tool_name(original_name)
        args = tool_call.get("args")
        result = tool_call.get("result")
        status = tool_call.get("status")

        # Parse JSON string args if needed
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                pass

        args = normalize_tool_input(original_name, canonical_name, args, msg)

        # Add tool use block
        tool_block: dict = {
            "type": "tool_use",
            "name": canonical_name,
            "input": args,
            "tool_call_id": tool_call.get("call_id"),
            "seq": seq,
        }
        if canonical_name != original_name:
            tool_block["provider_tool_name"] = original_name
        blocks.append(cast(ContentBlock, tool_block))
        seq += 1

        # Add tool result if present
        if result:
            result_content = result
            if isinstance(result, str):
                try:
                    result_content = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    pass

            blocks.append(cast(ContentBlock, {
                "type": "tool_result",
                "name": canonical_name,
                "content": result_content,
                "status": status,
                "user_decision": tool_call.get("user_decision"),
                "seq": seq,
            }))
            if canonical_name != original_name:
                cast(Dict[str, Any], blocks[-1])["provider_tool_name"] = original_name
            seq += 1
    return seq


def append_v2_thinking(
    blocks: List[ContentBlock], msg: Dict[str, Any], seq: int
) -> int:
    """Append the v2-export thinking block (only when ``thinking_duration_ms`` is present).

    This is the actual thinking text from thinking.text. Only used when
    thinking_duration_ms is present (the v2 indicator); otherwise the legacy
    handler (append_legacy_thinking) already covered it. Note both fire for
    a plain-string v2 thinking, by design preserved here.
    """
    thinking_text = msg.get("thinking")
    thinking_duration = msg.get("thinking_duration_ms")
    if thinking_text and isinstance(thinking_text, str) and thinking_duration is not None:
        blocks.append(cast(ContentBlock, {
            "type": "thinking",
            "text": thinking_text,
            "duration_ms": thinking_duration,
            "seq": seq,
        }))
        seq += 1
    return seq


def append_fallback(
    blocks: List[ContentBlock], fallback_content: Any, seq: int
) -> int:
    """Append text blocks from fallback content when no other blocks were built."""
    if isinstance(fallback_content, str):
        blocks.append(ProviderParser.create_text_block(fallback_content, seq))
    elif isinstance(fallback_content, list):
        for item in fallback_content:
            if isinstance(item, str):
                blocks.append(ProviderParser.create_text_block(item, seq))
                seq += 1
            elif isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    blocks.append(ProviderParser.create_text_block(text, seq))
                    seq += 1
    return seq


def normalize_tool_input(
    original_name: str,
    canonical_name: str,
    input_data: Any,
    msg: Dict[str, Any],
) -> Any:
    """Normalize tool input to Thread's canonical format.

    - Recovers file_path from code_blocks when input is null/missing
    - Normalizes provider-specific path fields (e.g. input.path → input.file_path)
    """
    if canonical_name not in FILE_TOOLS:
        return input_data

    # Ensure input is a dict
    if input_data is None:
        input_data = {}
    if not isinstance(input_data, dict):
        return input_data

    # Already has file_path — nothing to do
    if input_data.get("file_path"):
        return input_data

    # Try provider-specific path field (e.g. read_file_v2 uses "path")
    path_field = PROVIDER_PATH_FIELDS.get(original_name)
    if path_field and input_data.get(path_field):
        input_data["file_path"] = input_data[path_field]
        return input_data

    # Recover from code_blocks on the same message (Cursor v2 format)
    # code_blocks contain the files the tool operated on
    code_blocks = msg.get("code_blocks", [])
    if code_blocks:
        for cb in code_blocks:
            if isinstance(cb, dict):
                uri = cb.get("uri", {})
                if isinstance(uri, dict) and uri.get("path"):
                    input_data["file_path"] = uri["path"]
                    return input_data

    return input_data
