"""Pure content-block / message-list helpers for the Claude Code parser.

These are the stateless parsing helpers extracted out of
``ClaudeCodeParser`` (see ``claude_code.py``). They take their inputs
explicitly and call only the shared static helpers on ``ProviderParser``
(``create_text_block``), so they carry no instance state. ``ClaudeCodeParser``
keeps thin delegating methods that call into here, preserving the class
surface (interface + test-called private methods) byte-for-byte.

Behavior is identical to the in-class originals: every emitted dict / list /
content-block shape and return value is unchanged.
"""

import re
import uuid as _uuid
from typing import Any, Dict, List, Tuple, cast

from .base import ContentBlock, NormalizedMessage, ProviderParser
from .claude_code_ide import _extract_ide_context

# Regex for parsing old-format XML tool calls. Kept here (also re-exported as
# class attributes on ClaudeCodeParser for back-compat) since the XML parser
# that uses them now lives here.
FUNCTION_CALLS_SPLIT = re.compile(
    r'(<function_calls>.*?</function_calls>)', re.DOTALL
)
INVOKE_PATTERN = re.compile(
    r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL
)
PARAMETER_PATTERN = re.compile(
    r'<parameter\s+name="([^"]+)">(.*?)</parameter>', re.DOTALL
)


def extract_tool_result_text(content: Any) -> str:
    """Extract text from tool result content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    texts.append(text)
        return "\n".join(texts)
    return ""


def user_content_blocks_from_list(
    content: list,
) -> "tuple[List[ContentBlock], str, List[Dict[str, Any]]]":
    """Build content blocks from a user message's list content.

    Returns ``(content_blocks, content_text, ide_context_blocks)``. Handles
    the tool_result / image / text block shapes; text blocks also yield IDE
    context that the caller appends (renumbered) after the main blocks.
    """
    content_blocks: List[ContentBlock] = []
    content_text = ""
    ide_context_blocks: List[Dict[str, Any]] = []
    # Array of content blocks (tool results, text blocks)
    seq = 0
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "tool_result":
                tool_content = block.get("content", [])
                # Extract text from tool result content
                result_text = extract_tool_result_text(tool_content)
                content_blocks.append(cast(ContentBlock, {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": tool_content,
                    "text": result_text,
                    "is_error": block.get("is_error", False),
                    "seq": seq,
                }))
                seq += 1
            elif block_type == "image":
                # Preserve image blocks (base64-encoded screenshots, etc.)
                content_blocks.append(cast(ContentBlock, {
                    "type": "image",
                    "source": block.get("source", {}),
                    "seq": seq,
                }))
                seq += 1
            elif block_type == "text":
                text = block.get("text", "")
                if text:
                    # Extract IDE context from text blocks
                    cleaned_text, extracted_ide = _extract_ide_context(text)
                    ide_context_blocks.extend(extracted_ide)

                    if cleaned_text:
                        content_text = cleaned_text if not content_text else f"{content_text}\n{cleaned_text}"
                        content_blocks.append(ProviderParser.create_text_block(cleaned_text, seq))
                        seq += 1
            elif block_type:
                # Preserve unknown user block types — "Archivist, Not Filter"
                # (mirrors append_assistant_block). document / redacted_thinking /
                # mcp_tool_use / future kinds keep their own block instead of
                # vanishing from content_blocks (they'd otherwise survive only in
                # the raw provider_data.line blob).
                content_blocks.append(cast(ContentBlock, {
                    "type": block_type,
                    "raw": block,
                    "seq": seq,
                }))
                seq += 1
        elif isinstance(block, str):
            # A bare string inside a user content list — keep it as text rather
            # than skip it.
            if block:
                content_text = block if not content_text else f"{content_text}\n{block}"
                content_blocks.append(ProviderParser.create_text_block(block, seq))
                seq += 1
    return content_blocks, content_text, ide_context_blocks


def parse_xml_function_calls(
    raw_content: str, seq: int
) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """Parse old-format <function_calls> XML from string content.

    Old Claude Code sessions (pre-2026) stored tool calls as XML text
    within the assistant response string. This splits the string into
    interleaved text and tool_use content blocks.

    Returns:
        (content_blocks, content_text_parts, updated_seq)
    """
    content_blocks: List[Dict[str, Any]] = []
    content_text_parts: List[str] = []

    segments = FUNCTION_CALLS_SPLIT.split(raw_content)

    for segment in segments:
        if not segment.strip():
            continue

        if segment.startswith('<function_calls>'):
            # Parse each <invoke> within the block
            for match in INVOKE_PATTERN.finditer(segment):
                tool_name = match.group(1)
                invoke_body = match.group(2)

                # Extract parameters
                input_data = {}
                for param in PARAMETER_PATTERN.finditer(invoke_body):
                    input_data[param.group(1)] = param.group(2)

                # Deterministic ID from content
                tool_call_id = str(_uuid.uuid5(
                    _uuid.NAMESPACE_URL,
                    f"{tool_name}:{seq}:{list(input_data.values())[:1]}"
                ))

                content_blocks.append({
                    "type": "tool_use",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "input": input_data,
                    "seq": seq,
                })
                seq += 1
        else:
            # Text segment
            text = segment.strip()
            if text:
                content_text_parts.append(text)
                content_blocks.append(cast(Dict[str, Any], ProviderParser.create_text_block(text, seq)))
                seq += 1

    return content_blocks, content_text_parts, seq


def append_assistant_block(
    block: Dict[str, Any],
    content_blocks: List[ContentBlock],
    content_text_parts: List[str],
    seq: int,
) -> int:
    """Append one list-shaped assistant block; return the next seq value."""
    block_type = block.get("type", "")

    if block_type == "thinking":
        thinking_text = block.get("thinking", "")
        if thinking_text:
            content_blocks.append(cast(ContentBlock, {
                "type": "thinking",
                "text": thinking_text,
                "signature": block.get("signature"),
                "seq": seq,
            }))
            return seq + 1

    elif block_type == "text":
        text = block.get("text", "")
        if text:
            content_text_parts.append(text)
            content_blocks.append(ProviderParser.create_text_block(text, seq))
            return seq + 1

    elif block_type == "tool_use":
        content_blocks.append({
            "type": "tool_use",
            "tool_call_id": block.get("id"),
            "name": block.get("name", "unknown"),
            "input": block.get("input", {}),
            "seq": seq,
        })
        return seq + 1

    elif block_type:
        # Preserve unknown block types - "Archivist, Not Filter"
        content_blocks.append(cast(ContentBlock, {
            "type": block_type,
            "raw": block,
            "seq": seq,
        }))
        return seq + 1

    return seq


def assistant_content(
    raw_content: Any,
) -> Tuple[List[ContentBlock], List[str]]:
    """Build (content_blocks, content_text_parts) for an assistant message.

    Handles both the list-of-blocks shape (thinking/text/tool_use/unknown)
    and the legacy string shape (plain or old-format <function_calls> XML).
    """
    content_blocks: List[ContentBlock] = []
    content_text_parts: List[str] = []
    seq = 0

    if isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            seq = append_assistant_block(
                block, content_blocks, content_text_parts, seq
            )
    elif isinstance(raw_content, str):
        if '<function_calls>' in raw_content:
            blocks, text_parts, seq = parse_xml_function_calls(
                raw_content, seq
            )
            content_blocks.extend(cast(List[ContentBlock], blocks))
            content_text_parts.extend(text_parts)
        else:
            content_text_parts.append(raw_content)
            content_blocks.append(ProviderParser.create_text_block(raw_content, 0))

    return content_blocks, content_text_parts


def dedup_compaction_replays(
    messages: List[NormalizedMessage],
) -> List[NormalizedMessage]:
    """Drop compaction-replay duplicates.

    Claude Code context compaction replays earlier messages in the JSONL
    with new UUIDs but identical content+timestamps. Keep the first
    occurrence of each (role, content_text, created_at) for user/assistant.
    """
    seen: set[tuple[str, str, str | None, str | None]] = set()
    deduped: List[NormalizedMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        if role in ("user", "assistant"):
            content_text = msg.get("content_text", "")
            created_at = msg.get("created_at")
            # A `/model` switch has empty content_text and shares its timestamp with the
            # command line beside it — so include the switch target in the key, else the
            # marker-bearing turn collides with an empty one and is wrongly deduped away.
            model_change = next(
                (b.get("to_model") for b in msg.get("content_blocks", [])
                 if isinstance(b, dict) and b.get("type") == "model_change"),
                None,
            )
            key = (role, content_text, str(created_at) if created_at else None, model_change)
            if key in seen:
                continue
            seen.add(key)
        deduped.append(msg)
    return deduped


def apply_derived_title(messages: List[NormalizedMessage]) -> None:
    """Derive a conversation title from the first user message and apply it.

    ChatGPT has explicit titles; Claude Code doesn't, so derive from content
    (first line, max 100 chars, ellipsised when truncated). Mutates each
    message's ``conversation_title`` in place.
    """
    first_user_content = None
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content_text"):
            first_user_content = msg.get("content_text")
            break

    if first_user_content:
        # Truncate to reasonable title length (first line, max 100 chars)
        title = first_user_content.split("\n")[0][:100]
        if len(first_user_content) > 100 or "\n" in first_user_content:
            title = title.rstrip() + "..."
        # Update all messages with the conversation title
        for msg in messages:
            msg["conversation_title"] = title


def build_chain_indices(
    messages: List[NormalizedMessage],
) -> Tuple[Dict[str, NormalizedMessage], Dict[str, List[NormalizedMessage]]]:
    """Build (by_id, children_of) indices over the message list."""
    by_id: Dict[str, NormalizedMessage] = {}
    for m in messages:
        mid = m.get("provider_message_id")
        if mid:
            by_id[mid] = m

    children_of: Dict[str, List[NormalizedMessage]] = {}
    for m in messages:
        parent_id = m.get("provider_parent_id")
        if parent_id:
            if parent_id not in children_of:
                children_of[parent_id] = []
            children_of[parent_id].append(m)

    return by_id, children_of


def find_chain_roots(
    messages: List[NormalizedMessage],
    by_id: Dict[str, NormalizedMessage],
) -> List[NormalizedMessage]:
    """Find chain roots: assistant messages whose parent is NOT an assistant
    (i.e., parent is user, system, or doesn't exist)."""
    chain_roots = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        parent_id = m.get("provider_parent_id")
        if not parent_id:
            chain_roots.append(m)
        elif parent_id in by_id:
            parent = by_id[parent_id]
            if parent.get("role") != "assistant":
                chain_roots.append(m)
        else:
            # Parent not in this session, treat as root
            chain_roots.append(m)
    return chain_roots


def merge_chain(
    root: NormalizedMessage,
    children_of: Dict[str, List[NormalizedMessage]],
    to_remove: set,
) -> None:
    """BFS-collect all descendant assistant blocks into ``root`` in place,
    marking merged children in ``to_remove``."""
    root_id = root.get("provider_message_id")
    if not root_id or root_id in to_remove:
        return

    all_blocks = list(root.get("content_blocks", []))
    queue = children_of.get(root_id, [])[:]

    while queue:
        child = queue.pop(0)
        if child.get("role") != "assistant":
            continue
        child_id = child.get("provider_message_id")
        if child_id in to_remove:
            continue

        # Add this child's blocks
        all_blocks.extend(child.get("content_blocks", []))
        to_remove.add(child_id)

        # Add this child's children to queue
        queue.extend(children_of.get(cast(str, child_id), []))

    # Update root's blocks
    root["content_blocks"] = all_blocks


def coalesce_assistant_chains(
    messages: List[NormalizedMessage],
) -> List[NormalizedMessage]:
    """Coalesce linked assistant messages into single messages.

    In Claude Code JSONL, one assistant "turn" is stored as multiple lines
    linked by parentUuid (thinking -> text -> tool_use -> tool_use...).
    This merges them into a single message with combined content_blocks.
    """
    by_id, children_of = build_chain_indices(messages)
    chain_roots = find_chain_roots(messages, by_id)

    # For each chain root, collect all descendant assistant messages
    to_remove: set = set()
    for root in chain_roots:
        merge_chain(root, children_of, to_remove)

    # Return messages with merged ones removed
    return [
        m for m in messages
        if m.get("provider_message_id") not in to_remove
    ]
