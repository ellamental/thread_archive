"""
Active path transformer for ChatGPT.

ChatGPT conversations are tree-structured. The "active path" is the
main thread from root to current_node. This transformer marks messages
on or off the active path.
"""

from typing import Dict, List, Optional, Set

from ..pipeline.interfaces import RawMessage


class ActivePathTransformer:
    """Computes and marks the active conversation path.

    ChatGPT's current_node field identifies the leaf of the "active"
    conversation thread. This transformer:
    1. Walks from current_node to root to find the active path
    2. Marks each message with is_active_path flag

    This is run before coalescing so only active-path messages
    get coalesced into their parents.
    """

    def __init__(self, current_node: Optional[str] = None):
        """Initialize with optional current_node.

        Args:
            current_node: Leaf node ID of active path. If not provided,
                          tries to extract from messages' conversation metadata.
        """
        self.current_node = current_node

    def transform(self, messages: List[RawMessage]) -> List[RawMessage]:
        """Mark messages with active path status.

        Args:
            messages: Messages to transform

        Returns:
            Same messages with is_active_path updated
        """
        if not messages:
            return messages

        # Try to get current_node from conversation metadata
        current_node = self.current_node
        if not current_node:
            for msg in messages:
                conv_meta = msg.conversation_metadata
                if conv_meta and "current_node" in conv_meta:
                    current_node = conv_meta["current_node"]
                    break

        if not current_node:
            # No current_node - assume all messages are active
            return messages

        # Build parent lookup
        parent_map: Dict[str, Optional[str]] = {}
        for msg in messages:
            parent_map[msg.provider_message_id] = msg.provider_parent_id

        # Walk from current_node to root
        active_ids = self._build_active_path(current_node, parent_map)

        # Mark messages
        for msg in messages:
            msg.is_active_path = msg.provider_message_id in active_ids

        return messages

    def _build_active_path(
        self, current_node: str, parent_map: Dict[str, Optional[str]]
    ) -> Set[str]:
        """Walk from current_node to root to build active path set."""
        active_ids: Set[str] = set()
        node_id: Optional[str] = current_node
        hop_guard = 0

        while node_id and hop_guard < 10_000:
            active_ids.add(node_id)
            node_id = parent_map.get(node_id)
            hop_guard += 1

        return active_ids
