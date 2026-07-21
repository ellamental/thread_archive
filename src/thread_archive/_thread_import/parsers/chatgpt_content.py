"""
Pure content-extraction / message-parsing helpers for ChatGPTParser.

These are the stateless bodies of ChatGPTParser's helper methods: content-block
building, special-case dispatch, text extraction, primary-message classification,
and coalesce-candidate collection. They carry no instance state — the parser's
methods are thin delegators over these functions. Content-block construction uses
ProviderParser's static factory methods (create_text_block, etc.) directly, so
the emitted block shapes are byte-identical to the inlined originals.

Kept in a sibling module (not on the class) purely to keep chatgpt.py lean; the
class retains every method the base interface, registry, or tests reference.
"""

from typing import Any, Dict, List, Optional, cast

from .base import ContentBlock, ProviderParser

# Content types that indicate thinking/reasoning messages
THINKING_CONTENT_TYPES = {"thoughts", "analysis", "reasoning_recap"}

# Content types that indicate structural/scaffolding messages (not displayed)
STRUCTURAL_CONTENT_TYPES = {"code", "commentary", "metadata", "system"}

# Content types that represent system context
CONTEXT_CONTENT_TYPES = {"user_editable_context", "model_editable_context"}

# Content types whose real payload lives outside the generic `text` field:
# tether_browsing_display carries fetched page/search content under `result`
# (with an optional `summary`); tether_quote carries `text` plus source
# descriptors (url/domain/title).
TETHER_CONTENT_TYPES = {"tether_browsing_display", "tether_quote"}

# Block types the annotations convention models (the event builder copies
# `annotations` from these onto the block's event payload).
_ANNOTATABLE_BLOCK_TYPES = {"text", "thinking", "tool_use", "tool_result"}


def extract_text_from_content(content: Any) -> str:
    """Extract text content from ChatGPT content object."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Check for text field. Only trust a string value — a dict/None `text`
        # would otherwise be returned verbatim and crash downstream `.strip()`;
        # fall through to the parts handling (which yields "" if there are none).
        if isinstance(content.get("text"), str) and (
            content["text"] or content.get("content_type") not in TETHER_CONTENT_TYPES
        ):
            return content["text"]
        # Tether content keeps its real text under `result`/`summary` — without
        # this the block comes out empty and the fetched content is dropped.
        if content.get("content_type") in TETHER_CONTENT_TYPES:
            for key in ("result", "summary"):
                if isinstance(content.get(key), str) and content[key]:
                    return content[key]
        # Check for parts array
        parts = content.get("parts", [])
        text_parts = []
        for part in parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
        return "\n\n".join(text_parts)
    return ""


def extract_special_block(
    raw_msg: Dict,
    content: Any,
    content_type: str,
    role: str,
    msg_id: Optional[str],
    seq: int,
) -> Optional[tuple[List[ContentBlock], int]]:
    """Handle the exclusive content cases that fully consume a message.

    Returns ``(blocks, next_seq)`` when one applies, else ``None`` so the
    caller continues with the additive (tool_calls/text/image) sections.
    """
    # Handle stub messages
    if raw_msg.get("is_stub"):
        return [], seq

    # Handle tool role messages -> tool_result block
    if role == "tool":
        tool_name = raw_msg.get("author_name") or "unknown"
        result_block = ProviderParser.create_tool_result_block(
            tool_name,
            content,
            seq,
            provider_message_id=msg_id,
        )
        return [result_block], seq + 1

    # Handle thinking/reasoning content
    if content_type in THINKING_CONTENT_TYPES:
        text = extract_text_from_content(content)
        if text:
            thinking_block = ProviderParser.create_thinking_block(
                text,
                seq,
                thinking_type=content_type,
                provider_message_id=msg_id,
            )
            return [thinking_block], seq + 1

    # Handle system context content
    if content_type in CONTEXT_CONTENT_TYPES:
        text = extract_text_from_content(content)
        if text:
            ctx_block: ContentBlock = {
                "type": "system_context",
                "text": text,
                "context_type": content_type,
                "seq": seq,
            }
            return [ctx_block], seq + 1

    # Check for tool calls via recipient field (primary ChatGPT mechanism)
    # When recipient is set to a tool name (not "all"), the assistant is invoking a tool
    # and the text content is the tool input (e.g. Python code for the code interpreter)
    recipient = raw_msg.get("recipient")
    if recipient and recipient != "all" and role == "assistant":
        text = extract_text_from_content(content)
        tool_block = ProviderParser.create_tool_use_block(
            recipient,
            {"code": text} if text else {},
            seq,
            provider_message_id=msg_id,
        )
        return [tool_block], seq + 1

    return None


def append_metadata_tool_calls(
    blocks: List[ContentBlock],
    msg_metadata: Dict,
    msg_id: Optional[str],
    seq: int,
) -> int:
    """Append tool_use blocks from metadata tool_calls (alternate export formats)."""
    tool_calls = msg_metadata.get("tool_calls") or msg_metadata.get("tool_call")
    if tool_calls:
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    name = tc.get("name") or tc.get("function", {}).get("name") or "unknown"
                    args = tc.get("args") or tc.get("arguments") or tc.get("function", {}).get("arguments")
                    block = ProviderParser.create_tool_use_block(
                        name,
                        args,
                        seq,
                        provider_message_id=msg_id,
                    )
                    blocks.append(block)
                    seq += 1
        elif isinstance(tool_calls, dict):
            name = tool_calls.get("name") or tool_calls.get("function", {}).get("name") or "unknown"
            args = tool_calls.get("args") or tool_calls.get("arguments") or tool_calls.get("function", {}).get("arguments")
            block = ProviderParser.create_tool_use_block(
                name,
                args,
                seq,
                provider_message_id=msg_id,
            )
            blocks.append(block)
            seq += 1
    return seq


def append_text_content(blocks: List[ContentBlock], content: Any, seq: int) -> int:
    """Append a text block from regular content, if any."""
    text = extract_text_from_content(content)
    if text:
        block = ProviderParser.create_text_block(text, seq)
        blocks.append(block)
        seq += 1
    return seq


def append_image_parts(blocks: List[ContentBlock], content: Any, seq: int) -> int:
    """Append image blocks from multimodal content parts.

    ChatGPT emits two image shapes and this must catch both: an inline ``image/*``
    part, and — far more common — an ``image_asset_pointer`` part whose ``content_type``
    is the literal string ``"image_asset_pointer"`` and whose ``asset_pointer`` is a
    ``file-service://`` / ``sediment://`` reference to an uploaded or generated image.
    The pointer (plus size/dimensions) is preserved so the reference survives in truth
    even though the bytes live outside the export; missing the pointer shape drops
    image-only turns entirely (no text, no block → the whole turn vanishes on import)."""
    if not isinstance(content, dict):
        return seq
    for part in content.get("parts", []):
        if not isinstance(part, dict):
            continue
        ctype = part.get("content_type") or ""
        is_image = (
            ctype.startswith("image/")
            or ctype == "image_asset_pointer"
            or bool(part.get("asset_pointer"))
        )
        if not is_image:
            continue
        img_block: ContentBlock = {
            "type": "image",
            "asset_pointer": part.get("asset_pointer"),
            "mime_type": ctype or None,
            "seq": seq,
        }
        # The bytes aren't in the export; keep the dimensional metadata that is, so a
        # later re-download / audit can match the pointer to what it pointed at.
        meta = {k: part[k] for k in ("size_bytes", "width", "height") if part.get(k) is not None}
        if meta:
            cast(Dict[str, Any], img_block)["metadata"] = meta
        blocks.append(img_block)
        seq += 1
    return seq


def content_type_annotations(content: Any, content_type: str) -> Dict[str, Any]:
    """Source descriptors a content_type carries beside its text: a ``code``
    message's language/response_format_name, a tether message's url/domain/title.
    Attached as block annotations, never folded into content fields."""
    if not isinstance(content, dict):
        return {}
    keys: tuple[str, ...]
    if content_type == "code":
        keys = ("language", "response_format_name")
    elif content_type in TETHER_CONTENT_TYPES:
        keys = ("url", "domain", "title")
    else:
        return {}
    return {k: content[k] for k in keys if content.get(k) not in (None, "")}


def _annotate_first_block(
    blocks: List[ContentBlock], content: Any, content_type: str
) -> None:
    """Merge the message's content-type descriptors onto its first modeled block."""
    ann = content_type_annotations(content, content_type)
    if not ann:
        return
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in _ANNOTATABLE_BLOCK_TYPES:
            merged = dict(cast(Dict[str, Any], block).get("annotations") or {})
            merged.update(ann)
            cast(Dict[str, Any], block)["annotations"] = merged
            return


def extract_content_blocks(
    raw_msg: Dict,
    start_seq: int,
) -> tuple[List[ContentBlock], int]:
    """
    Extract content blocks from a single raw message.
    Returns (blocks, next_seq).
    """
    content = raw_msg.get("content", {})
    content_type = (raw_msg.get("content_type") or "").lower()
    role = (raw_msg.get("role") or "").lower()
    msg_id = raw_msg.get("provider_message_id")
    msg_metadata = raw_msg.get("msg_metadata", {})

    # Exclusive cases (stub / tool role / thinking / system context /
    # recipient tool call) each fully handle the message.
    special = extract_special_block(
        raw_msg, content, content_type, role, msg_id, start_seq
    )
    if special is not None:
        blocks, seq = special
    else:
        # Additive sections: a message can carry any combination of metadata
        # tool calls, regular text, and multimodal images, appended in that order.
        blocks = []
        seq = start_seq
        seq = append_metadata_tool_calls(blocks, msg_metadata, msg_id, seq)
        seq = append_text_content(blocks, content, seq)
        seq = append_image_parts(blocks, content, seq)
    _annotate_first_block(blocks, content, content_type)
    return blocks, seq


def build_content_blocks(
    primary_msg: Dict,
    coalesced_msgs: List[Dict],
) -> List[ContentBlock]:
    """
    Build content_blocks array from primary message and coalesced children.
    """
    blocks: List[ContentBlock] = []
    seq = 0

    # Extract content from primary message
    primary_blocks, seq = extract_content_blocks(primary_msg, seq)
    blocks.extend(primary_blocks)

    # Add blocks from coalesced messages
    for child_msg in coalesced_msgs:
        child_blocks, seq = extract_content_blocks(child_msg, seq)
        blocks.extend(child_blocks)

    return blocks


def is_primary_message(raw_msg: Dict) -> bool:
    """
    Determine if a message should be a primary row (vs coalesced into parent).

    Primary messages:
    - User messages with displayable content
    - Assistant messages with text content
    - System messages

    Non-primary (coalesce candidates):
    - Tool role messages
    - Thinking/reasoning messages (content_type in THINKING_CONTENT_TYPES)
    - Stubs
    """
    if raw_msg.get("is_stub"):
        return False

    role = (raw_msg.get("role") or "").lower()
    content_type = (raw_msg.get("content_type") or "").lower()

    # Tool messages are coalesced
    if role == "tool":
        return False

    # Thinking/reasoning messages are coalesced
    if content_type in THINKING_CONTENT_TYPES:
        return False

    # System context is coalesced
    if content_type in CONTEXT_CONTENT_TYPES:
        return False

    # Structural content is coalesced
    if content_type in STRUCTURAL_CONTENT_TYPES:
        return False

    # Check for actual displayable content
    content = raw_msg.get("content", {})
    if isinstance(content, dict):
        parts = content.get("parts", [])
        text = content.get("text", "")
        if not parts and not text:
            # No content to display
            return False

    return role in ("user", "assistant", "system")


def collect_coalesce_candidates(
    parent_id: str,
    children_by_parent: Dict[str, List[str]],
    msg_by_id: Dict[str, Dict],
    active_path_ids: Any,
) -> List[str]:
    """
    Recursively collect child messages that should be coalesced into parent.

    Only coalesce messages on the active path that are:
    - Tool messages
    - Thinking/reasoning messages

    Stop recursion at branch points (multiple children on different paths).
    """
    result: List[str] = []
    to_process = list(children_by_parent.get(parent_id, []))

    while to_process:
        child_id = to_process.pop(0)
        child_msg = msg_by_id.get(child_id)
        if not child_msg:
            continue

        # Only coalesce messages on the active path
        if child_id not in active_path_ids:
            continue

        # Check if this is a coalesce candidate
        role = (child_msg.get("role") or "").lower()
        content_type = (child_msg.get("content_type") or "").lower()

        should_coalesce = (
            role == "tool" or
            content_type in THINKING_CONTENT_TYPES or
            content_type in CONTEXT_CONTENT_TYPES or
            content_type in STRUCTURAL_CONTENT_TYPES or
            child_msg.get("is_stub")
        )

        if should_coalesce:
            result.append(child_id)
            # Continue to this message's children
            to_process.extend(children_by_parent.get(child_id, []))

    return result
