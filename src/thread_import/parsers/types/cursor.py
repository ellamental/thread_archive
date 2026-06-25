"""
TypedDict schemas for Cursor IDE chat export format.

Storage location (local machine, not devcontainers):
- macOS: ~/Library/Application Support/Cursor/User/workspaceStorage/
- Linux: ~/.config/Cursor/User/workspaceStorage/
- Windows: %APPDATA%\\Cursor\\User\\workspaceStorage\\

Each workspace has a hashed subdirectory containing state.vscdb (SQLite).

Export methods:
1. cursor-export CLI tool (recommended) -> ZIP with cursor_chats.json
2. Cursor UI: Chat menu -> Export Chat -> Markdown file (single chat only)

Key characteristics:
- Multiple format versions (v1 simple, v2 structured)
- Role normalization needed (human/ai/bot -> user/assistant)
- Timestamps may be ISO string or Unix (seconds/milliseconds)
- Linear conversations (no branching)
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict, Union


class CursorContentBlock(TypedDict, total=False):
    """A content block in a Cursor message.

    Block types:
    - text: Plain text content
    - tool_use: Tool/function call
    - tool_result: Tool/function result
    - thinking: Extended thinking (model-dependent)
    - code: Code block with language
    """
    type: str  # "text", "tool_use", "tool_result", "thinking", "code"
    text: Optional[str]  # For text and thinking blocks
    name: Optional[str]  # For tool_use and tool_result
    input: Optional[Dict[str, Any]]  # For tool_use
    content: Optional[Any]  # For tool_result
    is_error: Optional[bool]  # For tool_result
    code: Optional[str]  # For code blocks
    language: Optional[str]  # For code blocks


class CursorToolCall(TypedDict, total=False):
    """Tool call structure in Cursor messages."""
    name: str
    arguments: Optional[Union[str, Dict[str, Any]]]
    input: Optional[Dict[str, Any]]
    args: Optional[Union[str, Dict[str, Any]]]
    call_id: Optional[str]
    result: Optional[Any]
    status: Optional[str]
    user_decision: Optional[str]
    function: Optional[Dict[str, Any]]  # OpenAI-style nesting


class CursorCodeBlock(TypedDict, total=False):
    """Code block from v2 export format (cursor_export.py output)."""
    uri: Optional[Dict[str, Any]]  # {path: str, ...}
    version: Optional[int]
    codeBlockIdx: Optional[int]
    content: str
    languageId: Optional[str]
    language: Optional[str]


class CursorMessage(TypedDict, total=False):
    """A single message in a Cursor conversation."""
    id: Optional[str]
    role: str  # "user", "human", "assistant", "ai", "bot", "system", "tool"
    content: Union[str, List[Any]]  # String or array of content items
    content_blocks: Optional[List[CursorContentBlock]]
    created_at: Optional[Union[str, int, float]]  # ISO or Unix timestamp
    model: Optional[str]

    # Tool-related fields
    tool_calls: Optional[List[CursorToolCall]]
    toolCalls: Optional[List[CursorToolCall]]  # Alternative casing
    tool_results: Optional[List[Dict[str, Any]]]
    toolResults: Optional[List[Dict[str, Any]]]  # Alternative casing
    tool_call: Optional[CursorToolCall]  # v2 single tool call

    # Thinking
    thinking: Optional[Union[str, List[Any]]]
    reasoning: Optional[Union[str, List[Any]]]
    thinking_duration_ms: Optional[int]

    # Code blocks (v2 format)
    code_blocks: Optional[List[CursorCodeBlock]]


class CursorConversation(TypedDict, total=False):
    """A single Cursor conversation."""
    id: str
    title: Optional[str]
    created_at: Optional[Union[str, int, float]]
    updated_at: Optional[Union[str, int, float]]
    workspace_hash: Optional[str]
    workspace_name: Optional[str]
    workspace_uri: Optional[str]
    messages: List[CursorMessage]


class CursorExport(TypedDict, total=False):
    """Cursor export bundle from cursor-export CLI tool."""
    provider: Literal["cursor"]
    conversations: List[CursorConversation]
    export_metadata: Optional[Dict[str, Any]]
