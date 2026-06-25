"""
Tool coalescing transformer for ChatGPT.

ChatGPT exports tool calls, thinking, and results as separate messages
linked via parent_id. This transformer coalesces them into content_blocks
on the parent assistant message.
"""

from typing import Any, Dict, List, Set, cast

from ..base import ContentBlock
from ..pipeline.interfaces import RawMessage

# Content types that indicate thinking/reasoning
THINKING_CONTENT_TYPES = {"thoughts", "analysis", "reasoning_recap"}

# Content types that indicate structural/scaffolding messages
STRUCTURAL_CONTENT_TYPES = {"code", "commentary", "metadata", "system"}

# Content types that represent system context
CONTEXT_CONTENT_TYPES = {"user_editable_context", "model_editable_context"}


class ToolCoalescingTransformer:
    """Coalesces tool/thinking messages into parent assistant messages.

    ChatGPT's export format stores:
    - Tool calls as separate assistant messages with tool_calls in metadata
    - Tool results as separate tool-role messages
    - Thinking as separate messages with content_type in THINKING_CONTENT_TYPES
    - System context as separate messages with content_type in CONTEXT_CONTENT_TYPES

    This transformer:
    1. Identifies "primary" messages (user/assistant with displayable content)
    2. Finds child messages that should be coalesced (tool, thinking, context)
    3. Converts coalesced messages to content_blocks on the parent
    4. Removes coalesced messages from the output list

    Only messages on the active path are coalesced. Branch messages remain separate.
    """

    def transform(self, messages: List[RawMessage]) -> List[RawMessage]:
        """Coalesce tool and thinking messages into parents.

        Args:
            messages: Messages to transform (should have is_active_path set)

        Returns:
            Transformed messages with coalesced content
        """
        if not messages:
            return messages

        # Build lookup structures
        msg_by_id: Dict[str, RawMessage] = {
            m.provider_message_id: m for m in messages
        }
        children_by_parent: Dict[str, List[str]] = {}
        for msg in messages:
            if msg.provider_parent_id:
                children_by_parent.setdefault(msg.provider_parent_id, []).append(
                    msg.provider_message_id
                )

        # Sort by message_order for consistent processing
        sorted_messages = sorted(messages, key=lambda m: m.message_order or 0)

        result: List[RawMessage] = []
        coalesced_ids: Set[str] = set()

        for msg in sorted_messages:
            if msg.provider_message_id in coalesced_ids:
                continue

            if self._is_primary_message(msg):
                # Collect children to coalesce
                children_to_coalesce = self._collect_coalesce_candidates(
                    msg.provider_message_id,
                    children_by_parent,
                    msg_by_id,
                )

                # Mark as coalesced
                for child_id in children_to_coalesce:
                    coalesced_ids.add(child_id)

                # Build content blocks from primary + children
                self._build_content_blocks(
                    msg,
                    [msg_by_id[cid] for cid in children_to_coalesce if cid in msg_by_id],
                )

                result.append(msg)
            else:
                # Non-primary that wasn't coalesced - keep as separate
                result.append(msg)

        return result

    def _is_primary_message(self, msg: RawMessage) -> bool:
        """Determine if message should be a primary row.

        Primary messages:
        - User messages with displayable content
        - Assistant messages with text content
        - System messages

        Non-primary (coalesce candidates):
        - Tool role messages
        - Thinking/reasoning messages
        - Stubs
        """
        if msg.is_stub:
            return False

        role = (msg.role or "").lower()
        content_type = (msg.content_type or "").lower()

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
        content = msg.content
        if isinstance(content, dict):
            parts = content.get("parts", [])
            text = content.get("text", "")
            if not parts and not text:
                return False

        return role in ("user", "assistant", "system")

    def _collect_coalesce_candidates(
        self,
        parent_id: str,
        children_by_parent: Dict[str, List[str]],
        msg_by_id: Dict[str, RawMessage],
    ) -> List[str]:
        """Recursively collect child messages to coalesce.

        Only coalesces messages on the active path that are:
        - Tool messages
        - Thinking/reasoning messages
        - Context messages
        - Structural messages
        """
        result: List[str] = []
        to_process = list(children_by_parent.get(parent_id, []))

        while to_process:
            child_id = to_process.pop(0)
            child_msg = msg_by_id.get(child_id)
            if not child_msg:
                continue

            # Only coalesce active path messages
            if not child_msg.is_active_path:
                continue

            role = (child_msg.role or "").lower()
            content_type = (child_msg.content_type or "").lower()

            should_coalesce = (
                role == "tool"
                or content_type in THINKING_CONTENT_TYPES
                or content_type in CONTEXT_CONTENT_TYPES
                or content_type in STRUCTURAL_CONTENT_TYPES
                or child_msg.is_stub
            )

            if should_coalesce:
                result.append(child_id)
                # Continue to this message's children
                to_process.extend(children_by_parent.get(child_id, []))

        return result

    def _build_content_blocks(
        self, primary: RawMessage, coalesced: List[RawMessage]
    ) -> None:
        """Build content_blocks from primary and coalesced messages.

        Modifies primary.content_blocks in place.
        """
        blocks: List[ContentBlock] = []
        seq = 0

        # Extract from primary
        primary_blocks, seq = self._extract_blocks(primary, seq)
        blocks.extend(primary_blocks)

        # Add from coalesced
        for child in coalesced:
            child_blocks, seq = self._extract_blocks(child, seq)
            blocks.extend(child_blocks)

        primary.content_blocks = blocks

    def _extract_blocks(
        self, msg: RawMessage, start_seq: int
    ) -> tuple[List[ContentBlock], int]:
        """Extract content blocks from a message."""
        if msg.is_stub:
            return [], start_seq

        content = msg.content
        content_type = (msg.content_type or "").lower()
        role = (msg.role or "").lower()
        msg_id = msg.provider_message_id
        metadata = msg.metadata

        # Exclusive cases (tool role / thinking / system context) each fully
        # handle the message and return early.
        special = self._extract_special_blocks(
            content, content_type, role, msg_id, metadata, start_seq
        )
        if special is not None:
            return special

        # Additive sections: metadata tool calls, then regular text, then images.
        blocks: List[ContentBlock] = []
        seq = start_seq
        seq = self._append_metadata_tool_calls(blocks, metadata, msg_id, seq)
        seq = self._append_text(blocks, content, seq)
        seq = self._append_images(blocks, content, seq)
        return blocks, seq

    def _extract_special_blocks(
        self,
        content: Any,
        content_type: str,
        role: str,
        msg_id: str,
        metadata: Dict[str, Any],
        seq: int,
    ) -> "tuple[List[ContentBlock], int] | None":
        """Handle the exclusive content cases that fully consume a message.

        Returns ``(blocks, next_seq)`` when one applies, else ``None`` so the
        caller continues with the additive (tool_calls/text/image) sections.
        """
        # Tool role -> tool_result
        if role == "tool":
            tool_name = metadata.get("author_name") or "unknown"
            return [{
                "type": "tool_result",
                "name": tool_name,
                "content": content,
                "seq": seq,
                "provider_message_id": msg_id,
            }], seq + 1

        # Thinking content
        if content_type in THINKING_CONTENT_TYPES:
            text = self._extract_text(content)
            if text:
                return [cast(ContentBlock, {
                    "type": "thinking",
                    "text": text,
                    "thinking_type": content_type,
                    "seq": seq,
                    "provider_message_id": msg_id,
                })], seq + 1

        # System context
        if content_type in CONTEXT_CONTENT_TYPES:
            text = self._extract_text(content)
            if text:
                return [{
                    "type": "system_context",
                    "text": text,
                    "context_type": content_type,
                    "seq": seq,
                }], seq + 1

        return None

    def _append_metadata_tool_calls(
        self,
        blocks: List[ContentBlock],
        metadata: Dict[str, Any],
        msg_id: str,
        seq: int,
    ) -> int:
        """Append tool_use blocks from metadata tool_calls."""
        tool_calls = metadata.get("tool_calls") or metadata.get("tool_call")
        if tool_calls:
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = (
                            tc.get("name")
                            or tc.get("function", {}).get("name")
                            or "unknown"
                        )
                        args = (
                            tc.get("args")
                            or tc.get("arguments")
                            or tc.get("function", {}).get("arguments")
                        )
                        blocks.append({
                            "type": "tool_use",
                            "name": name,
                            "input": args,
                            "seq": seq,
                            "provider_message_id": msg_id,
                        })
                        seq += 1
            elif isinstance(tool_calls, dict):
                name = (
                    tool_calls.get("name")
                    or tool_calls.get("function", {}).get("name")
                    or "unknown"
                )
                args = (
                    tool_calls.get("args")
                    or tool_calls.get("arguments")
                    or tool_calls.get("function", {}).get("arguments")
                )
                blocks.append({
                    "type": "tool_use",
                    "name": name,
                    "input": args,
                    "seq": seq,
                    "provider_message_id": msg_id,
                })
                seq += 1
        return seq

    def _append_text(
        self, blocks: List[ContentBlock], content: Any, seq: int
    ) -> int:
        """Append a text block from regular content, if any."""
        text = self._extract_text(content)
        if text:
            blocks.append({
                "type": "text",
                "text": text,
                "seq": seq,
            })
            seq += 1
        return seq

    def _append_images(
        self, blocks: List[ContentBlock], content: Any, seq: int
    ) -> int:
        """Append image blocks from multimodal content parts."""
        if isinstance(content, dict):
            parts = content.get("parts", [])
            for part in parts:
                if isinstance(part, dict):
                    if part.get("content_type", "").startswith("image/"):
                        blocks.append({
                            "type": "image",
                            "asset_pointer": part.get("asset_pointer"),
                            "mime_type": part.get("content_type"),
                            "seq": seq,
                        })
                        seq += 1
        return seq

    def _extract_text(self, content: Any) -> str:
        """Extract text from various content formats."""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if "text" in content:
                return content["text"]
            parts = content.get("parts", [])
            text_parts = []
            for part in parts:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
            return "\n\n".join(text_parts)
        return ""
