"""Pure per-message-shape parsing helpers for the Cursor exporter.

Stateless functions that keep ``CursorExporter`` thin. The emitted dict/list
shapes are an import contract and must stay byte-for-byte stable.

No filesystem, no SQLite, no instance state — pure transforms over the raw
composer/bubble/message blobs that Cursor stores. ``CursorExporter``'s pinned
methods delegate here.
"""

from typing import Any, Dict, List, Optional


def kv_role(bubble: Dict[str, Any], header: Dict[str, Any]) -> str:
    """Map a Cursor bubble/header ``type`` (1/2) onto a canonical role."""
    bubble_type = bubble.get("type") or header.get("type")
    if bubble_type == 1:
        return "user"
    if bubble_type == 2:
        return "assistant"
    return "unknown"


def build_kv_message(
    bubble_id: str,
    bubble: Dict[str, Any],
    header: Dict[str, Any],
    idx: int,
) -> Dict[str, Any]:
    """Build one conversation message from a composer header + its bubble."""
    message = {
        "id": bubble_id,
        "role": kv_role(bubble, header),
        "content": bubble.get("text", ""),
        "created_at": bubble.get("createdAt"),
        "index": idx,
    }

    tool_data = bubble.get("toolFormerData")
    if tool_data and isinstance(tool_data, dict):
        message["tool_call"] = {
            "name": tool_data.get("name", "unknown"),
            "tool_id": tool_data.get("tool"),
            "call_id": tool_data.get("toolCallId"),
            "status": tool_data.get("status"),
            "args": tool_data.get("rawArgs"),
            "params": tool_data.get("params"),
            "result": tool_data.get("result"),
            "user_decision": tool_data.get("userDecision"),
        }

    code_blocks = bubble.get("codeBlocks", [])
    if code_blocks:
        message["code_blocks"] = code_blocks

    thinking = bubble.get("thinking")
    if thinking and isinstance(thinking, dict):
        thinking_text = thinking.get("text", "")
        if thinking_text:
            message["thinking"] = thinking_text
            message["thinking_duration_ms"] = bubble.get("thinkingDurationMs")

    if bubble.get("serverBubbleId"):
        message["server_bubble_id"] = bubble["serverBubbleId"]

    return message


def build_kv_metadata(composer: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the conversation metadata block from a composer record."""
    return {
        "mode": composer.get("unifiedMode"),
        "status": composer.get("status"),
        "is_agentic": composer.get("isAgentic"),
        "context_tokens": composer.get("contextTokensUsed"),
        "lines_added": composer.get("totalLinesAdded"),
        "lines_removed": composer.get("totalLinesRemoved"),
        "branch": composer.get("createdOnBranch"),
    }


def normalize_role(msg: Dict[str, Any]) -> Optional[str]:
    """Map Cursor's role/type/sender aliases onto canonical role names."""
    role = msg.get("role") or msg.get("type") or msg.get("sender")
    if role:
        role = role.lower()
        if role in ("human", "user"):
            role = "user"
        elif role in ("assistant", "ai", "bot"):
            role = "assistant"
        elif role in ("system",):
            role = "system"
        elif role in ("tool", "function"):
            role = "tool"
    return role


def content_blocks_from_content(raw_content: Any) -> List[Dict[str, Any]]:
    """Build text content blocks from a message's raw ``content`` field."""
    content_blocks: List[Dict[str, Any]] = []
    if isinstance(raw_content, list):
        for block in raw_content:
            if isinstance(block, dict):
                content_blocks.append(block)
            elif isinstance(block, str):
                content_blocks.append({"type": "text", "text": block})
    elif isinstance(raw_content, str):
        content_blocks.append({"type": "text", "text": raw_content})
    return content_blocks


def parse_message(msg: Any, index: int = 0) -> Optional[Dict[str, Any]]:
    """Parse a single message from Cursor's format."""
    if not isinstance(msg, dict):
        return None

    role = normalize_role(msg)
    content = msg.get("content") or msg.get("text") or msg.get("message") or ""
    content_blocks = content_blocks_from_content(msg.get("content"))

    tool_calls = msg.get("tool_calls") or msg.get("toolCalls") or msg.get("function_call")
    tool_results = msg.get("tool_results") or msg.get("toolResults")
    thinking = msg.get("thinking") or msg.get("reasoning")

    parsed = {
        "id": msg.get("id") or msg.get("messageId") or f"msg_{index}",
        "role": role or "unknown",
        "content": content if isinstance(content, str) else "",
        "content_blocks": content_blocks,
        "created_at": msg.get("createdAt") or msg.get("created_at") or msg.get("timestamp"),
        "index": index,
        "raw_data": msg,
    }

    if tool_calls:
        parsed["tool_calls"] = tool_calls
    if tool_results:
        parsed["tool_results"] = tool_results
    if thinking:
        parsed["thinking"] = thinking

    if msg.get("model"):
        parsed["model"] = msg["model"]

    return parsed
