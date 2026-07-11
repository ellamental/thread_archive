"""
Thinking merge transformer for Claude Code.

In Claude Code exports, thinking-only messages (messages with only thinking
blocks and no text) should be merged into their parent assistant message's
content_blocks.
"""

from typing import Dict, List, Set

from ..pipeline.interfaces import RawMessage


class ThinkingMergeTransformer:
    """Merges thinking-only messages into parent assistant messages.

    In the normalized data model, thinking blocks should be nested within
    the parent message's content_blocks, not as separate messages.

    This transformer:
    1. Identifies messages with only thinking blocks (no text)
    2. Appends their thinking blocks to the parent's content_blocks
    3. Removes the thinking-only messages from output

    Original provider data is preserved in provider_data.
    """

    def transform(self, messages: List[RawMessage]) -> List[RawMessage]:
        """Merge thinking-only messages into parents.

        Args:
            messages: Messages to transform

        Returns:
            Messages with thinking blocks merged
        """
        if not messages:
            return messages

        # Build lookup by ID
        msg_by_id: Dict[str, RawMessage] = {
            m.provider_message_id: m for m in messages
        }

        to_remove: Set[str] = set()

        for msg in messages:
            blocks = msg.content_blocks
            has_text = any(
                b.get("type") == "text" and b.get("text") for b in blocks
            )
            has_thinking = any(b.get("type") == "thinking" for b in blocks)

            # If message has thinking but no text, merge into parent
            if has_thinking and not has_text:
                parent_id = msg.provider_parent_id
                if parent_id and parent_id in msg_by_id:
                    parent = msg_by_id[parent_id]
                    # Only merge into assistant messages
                    if parent.role == "assistant":
                        # Append thinking blocks to parent
                        for block in blocks:
                            if block.get("type") == "thinking":
                                parent.content_blocks.append(block)
                        to_remove.add(msg.provider_message_id)

        # Remove merged messages
        return [m for m in messages if m.provider_message_id not in to_remove]
