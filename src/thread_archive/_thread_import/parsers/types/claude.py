"""
TypedDict schemas for Claude.ai export format.

Export source: Claude.ai -> Settings -> Account -> Export Data
File structure: ZIP archive containing:
- conversations.json - Chat conversations with messages (primary)
- memories.json - Claude's memory about the user (optional)
- projects.json - Claude Projects and attached docs (optional)
- users.json - Account information (optional)

Key differences from other providers:
- No thinking blocks (server-side only, not included in exports)
- No branching (strictly linear conversations)
- Uses "human" role where others use "user"
"""

from typing import Any, Dict, List, Optional, TypedDict


class ClaudeContentBlock(TypedDict, total=False):
    """A content block in a Claude message.

    Block types:
    - text: Plain text content
    - tool_use: Tool/function call
    - tool_result: Tool/function result
    - thinking: Extended thinking (NOT present in web exports)
    - token_budget: Internal token tracking
    """
    type: str  # "text", "tool_use", "tool_result", "thinking", "token_budget"
    text: Optional[str]  # For text blocks
    thinking: Optional[str]  # For thinking blocks
    signature: Optional[str]  # For thinking blocks
    name: Optional[str]  # For tool_use and tool_result
    input: Optional[Dict[str, Any]]  # For tool_use
    content: Optional[Any]  # For tool_result
    is_error: Optional[bool]  # For tool_result
    display_content: Optional[Any]
    start_timestamp: Optional[str]
    stop_timestamp: Optional[str]


class ClaudeAttachment(TypedDict, total=False):
    """File attachment in a Claude message."""
    id: str
    file_name: str
    file_type: str
    file_size: Optional[int]
    extracted_content: Optional[str]


class ClaudeMessage(TypedDict, total=False):
    """A single message in a Claude conversation."""
    uuid: str
    text: str  # Legacy field, may exist alongside content
    content: List[ClaudeContentBlock]  # Structured content blocks
    sender: str  # "human" or "assistant"
    created_at: str  # ISO timestamp
    updated_at: Optional[str]  # ISO timestamp
    attachments: List[ClaudeAttachment]
    files: List[Dict[str, Any]]


class ClaudeAccount(TypedDict, total=False):
    """Account reference in a conversation."""
    uuid: str


class ClaudeProject(TypedDict, total=False):
    """Project reference in a conversation."""
    uuid: str


class ClaudeConversation(TypedDict, total=False):
    """A single Claude conversation."""
    uuid: str
    name: str
    summary: Optional[str]
    created_at: str
    updated_at: str
    account: ClaudeAccount
    project: Optional[ClaudeProject]
    chat_messages: List[ClaudeMessage]


class ClaudeMemory(TypedDict, total=False):
    """User memory from Claude export."""
    content: str
    created_at: str
    updated_at: str


class ClaudeMemories(TypedDict, total=False):
    """Memories section of Claude export."""
    user_memories: List[ClaudeMemory]
    project_memories: Dict[str, str]  # project_uuid -> memory text


class ClaudeProjectFull(TypedDict, total=False):
    """Full project information from Claude export."""
    uuid: str
    name: str
    description: Optional[str]
    created_at: str
    updated_at: str


class ClaudeUser(TypedDict, total=False):
    """User information from Claude export."""
    uuid: str
    full_name: str
    email: Optional[str]


class ClaudeExport(TypedDict, total=False):
    """Complete Claude data export bundle."""
    conversations: List[ClaudeConversation]
    memories: Optional[ClaudeMemories]
    projects: Optional[List[ClaudeProjectFull]]
    users: Optional[List[ClaudeUser]]
