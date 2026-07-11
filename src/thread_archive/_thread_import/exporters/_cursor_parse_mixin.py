"""Composer/chat-blob parsing methods for ``CursorExporter``.

A mixin of the stateless ``_parse_*`` / ``_normalize_role`` /
``_content_blocks_from_content`` / ``_extract_chat_data`` methods, lifted out of
``CursorExporter`` so the class file stays focused. These read no instance state
(no ``self.storage_path``) — they transform loose composer/chat blobs into
conversation dicts, delegating the per-message shape work to ``cursor_parse``.

The methods stay methods (not free functions) so callers that access them as
class/instance attributes — including tests that build a bare exporter with
``__new__`` and the ``export_all`` orchestrator that stubs them on the instance —
keep resolving to the same objects via the assembled class's MRO. Moved
byte-for-byte from ``cursor.py``.
"""

import re
from typing import Any, Dict, List, Optional

from . import cursor_parse


class CursorParseMixin:
    """Stateless composer/chat-blob → conversation parsing methods."""

    def _extract_chat_data(self, db_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract chat-related data from the database dump."""
        chat_data = {}

        chat_patterns = [
            r".*chat.*",
            r".*composer.*",
            r".*conversation.*",
            r".*aiContext.*",
            r".*ai\..*",
            r".*cursor\..*",
            r".*workbench\.panel\.aichat.*",
            r".*workbench\.panel\.chat.*",
        ]

        for key, value in db_data.items():
            key_lower = key.lower()
            for pattern in chat_patterns:
                if re.match(pattern, key_lower, re.IGNORECASE):
                    chat_data[key] = value
                    break

        return chat_data

    def _parse_composer_data(self, data: Any) -> List[Dict[str, Any]]:
        """Parse Cursor's composer/chat data into conversations."""
        if not data:
            return []

        if isinstance(data, dict):
            return self._parse_composer_dict(data)

        if isinstance(data, list):
            return self._parse_conversation_items(data)

        return []

    def _parse_composer_dict(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse a dict-shaped composer blob: keyed conversation collections,
        falling back to treating the dict itself as a single conversation."""
        conversations: List[Dict[str, Any]] = []

        for key in ["tabs", "conversations", "chats", "composerData", "allTabs"]:
            if key in data:
                conversations.extend(self._parse_keyed_collection(data[key]))

        if not conversations and ("messages" in data or "bubbles" in data or "conversation" in data):
            conv = self._parse_single_conversation(data)
            if conv:
                conversations.append(conv)

        return conversations

    def _parse_keyed_collection(self, items: Any) -> List[Dict[str, Any]]:
        """Parse one keyed collection (list of conversations or id->conversation
        dict) into a list of conversations."""
        if isinstance(items, list):
            return self._parse_conversation_items(items)
        if isinstance(items, dict):
            conversations: List[Dict[str, Any]] = []
            for conv_id, item in items.items():
                conv = self._parse_single_conversation(item, conv_id)
                if conv:
                    conversations.append(conv)
            return conversations
        return []

    def _parse_conversation_items(self, items: List[Any]) -> List[Dict[str, Any]]:
        """Parse a plain list of conversation/tab structures, dropping any that
        don't yield a conversation."""
        conversations: List[Dict[str, Any]] = []
        for item in items:
            conv = self._parse_single_conversation(item)
            if conv:
                conversations.append(conv)
        return conversations

    def _parse_single_conversation(self, data: Any, conv_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Parse a single conversation/tab structure."""
        if not isinstance(data, dict):
            return None

        conversation = {
            "id": conv_id or data.get("id") or data.get("tabId") or data.get("conversationId"),
            "title": data.get("title") or data.get("name") or "Untitled",
            "created_at": data.get("createdAt") or data.get("created_at") or data.get("timestamp"),
            "updated_at": data.get("updatedAt") or data.get("updated_at"),
            "messages": [],
            "raw_data": data,
        }

        messages = []
        for msg_key in ["messages", "bubbles", "conversation", "chatMessages", "chat_messages"]:
            if msg_key in data:
                msg_data = data[msg_key]
                if isinstance(msg_data, list):
                    messages = msg_data
                    break
                elif isinstance(msg_data, dict) and "messages" in msg_data:
                    messages = msg_data["messages"]
                    break

        for i, msg in enumerate(messages):
            parsed_msg = self._parse_message(msg, i)
            if parsed_msg:
                conversation["messages"].append(parsed_msg)

        if conversation["messages"] or conversation["id"]:
            return conversation

        return None

    def _parse_message(self, msg: Any, index: int = 0) -> Optional[Dict[str, Any]]:
        """Parse a single message from Cursor's format."""
        return cursor_parse.parse_message(msg, index)

    @staticmethod
    def _normalize_role(msg: Dict[str, Any]) -> Optional[str]:
        """Map Cursor's role/type/sender aliases onto canonical role names."""
        return cursor_parse.normalize_role(msg)

    @staticmethod
    def _content_blocks_from_content(raw_content: Any) -> List[Dict[str, Any]]:
        """Build text content blocks from a message's raw ``content`` field."""
        return cursor_parse.content_blocks_from_content(raw_content)
