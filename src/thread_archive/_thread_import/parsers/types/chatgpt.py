"""
TypedDict schemas for ChatGPT export format.

Export source: ChatGPT web UI -> Settings -> Data controls -> Export data
File structure: ZIP archive containing conversations.json

The conversations.json contains an array of conversation objects with:
- mapping: Dict of message_id -> message node (tree structure)
- current_node: ID of the leaf node in the "active" conversation path
- title, create_time, update_time: Conversation metadata
"""

from typing import Any, Dict, List, Optional, TypedDict


class ChatGPTAuthor(TypedDict, total=False):
    """Author information for a ChatGPT message."""
    role: str  # "user", "assistant", "system", "tool"
    name: Optional[str]  # Tool/plugin name when role="tool"
    metadata: Dict[str, Any]


class ChatGPTContent(TypedDict, total=False):
    """Content structure for a ChatGPT message.

    ChatGPT uses various content_type values:
    - "text": Regular text content
    - "thoughts", "analysis", "reasoning_recap": Thinking blocks (o1 models)
    - "user_editable_context", "model_editable_context": Custom instructions
    - "code", "commentary", "metadata", "system": Structural content
    """
    content_type: str
    parts: List[Any]  # Array of content parts (strings, dicts)
    text: Optional[str]  # Alternative to parts array


class ChatGPTMessageMetadata(TypedDict, total=False):
    """Metadata for a ChatGPT message."""
    model_slug: Optional[str]
    model: Optional[str]
    is_visually_hidden_from_conversation: Optional[bool]
    is_user_system_message: Optional[bool]
    parent_id: Optional[str]
    content_type: Optional[str]
    tool_calls: Optional[List[Dict[str, Any]]]
    tool_call: Optional[Dict[str, Any]]


class ChatGPTMessage(TypedDict, total=False):
    """A single message in a ChatGPT conversation."""
    id: str
    author: ChatGPTAuthor
    content: ChatGPTContent
    create_time: Optional[float]  # Unix timestamp
    update_time: Optional[float]  # Unix timestamp
    metadata: ChatGPTMessageMetadata
    status: Optional[str]
    end_turn: Optional[bool]
    weight: Optional[float]
    recipient: Optional[str]


class ChatGPTNode(TypedDict, total=False):
    """A node in the ChatGPT message tree (from mapping field)."""
    id: str
    parent: Optional[str]  # Parent node ID
    children: List[str]  # Child node IDs
    message: Optional[ChatGPTMessage]  # None for stub nodes


class ChatGPTConversation(TypedDict, total=False):
    """A single ChatGPT conversation."""
    id: str
    title: str
    create_time: float
    update_time: float
    mapping: Dict[str, ChatGPTNode]  # message_id -> node
    current_node: Optional[str]  # Leaf of active path
    moderation_results: Optional[List[Any]]
    plugin_ids: Optional[List[str]]
    gizmo_id: Optional[str]
    conversation_template_id: Optional[str]
    safe_urls: Optional[List[str]]
    is_archived: Optional[bool]


class ChatGPTUser(TypedDict, total=False):
    """User information from ChatGPT export."""
    id: str
    name: str
    email: str


class ChatGPTExport(TypedDict, total=False):
    """Complete ChatGPT data export bundle."""
    conversations: List[ChatGPTConversation]
    user: Optional[ChatGPTUser]
    message_feedback: Optional[Dict[str, Any]]
    model_comparisons: Optional[List[Any]]
