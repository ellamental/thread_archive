"""
TypedDict schemas for Claude Code CLI session format.

Storage location: ~/.claude/projects/<project-path>/<session-id>.jsonl
Where <project-path> is the working directory with / replaced by -
Example: /home/user/project -> -home-user-project

Sessions are JSONL (one JSON object per line) with message types:
- type: "user" - User messages with message.content
- type: "assistant" - Assistant responses with content blocks
- type: "system" - System events (commands, file snapshots, compaction)
- type: "summary" - Context continuation summaries
- type: "file-history-snapshot" - File state snapshots

Key characteristics:
- Model-specific thinking (Opus has extended thinking, Sonnet/Haiku don't)
- Context compaction via compact_boundary system messages
- IDE context embedded as XML tags in user messages
- Tool results in user messages (not coalesced like ChatGPT)
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict, Union


class ClaudeCodeContentBlock(TypedDict, total=False):
    """A content block in a Claude Code message.

    Block types mirror Claude's API:
    - text: Plain text content
    - thinking: Extended thinking (signature-verified)
    - tool_use: Tool call
    - tool_result: Tool execution result
    """
    type: str  # "text", "thinking", "tool_use", "tool_result"
    text: Optional[str]  # For text blocks
    thinking: Optional[str]  # For thinking blocks
    signature: Optional[str]  # For thinking blocks
    id: Optional[str]  # For tool_use
    name: Optional[str]  # For tool_use
    input: Optional[Dict[str, Any]]  # For tool_use
    tool_use_id: Optional[str]  # For tool_result
    content: Optional[Any]  # For tool_result


class ClaudeCodeUsage(TypedDict, total=False):
    """Token usage information."""
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: Optional[int]
    cache_read_input_tokens: Optional[int]


class ClaudeCodeMessage(TypedDict, total=False):
    """The message object within a Claude Code line.

    For user messages: content is a string or List[ContentBlock]
    For assistant messages: content is List[ContentBlock]
    """
    id: Optional[str]
    role: str  # "user" or "assistant"
    content: Union[str, List[ClaudeCodeContentBlock]]
    model: Optional[str]
    stop_reason: Optional[str]
    usage: Optional[ClaudeCodeUsage]


class ClaudeCodeCompactMetadata(TypedDict, total=False):
    """Metadata for context compaction events."""
    preTokens: int
    postTokens: int
    summarizedMessages: List[str]


class ClaudeCodeLine(TypedDict, total=False):
    """A single line/record in a Claude Code JSONL session file.

    type determines the structure:
    - "user": User message with message.content
    - "assistant": Assistant response with message.content blocks
    - "system": System event (command, compaction boundary)
    - "summary": Context continuation summary
    - "file-history-snapshot": File state snapshot
    """
    type: str  # "user", "assistant", "system", "summary", "file-history-snapshot"
    uuid: str
    parentUuid: Optional[str]
    sessionId: str
    timestamp: str  # ISO 8601
    message: Optional[ClaudeCodeMessage]

    # System message fields
    subtype: Optional[str]  # "compact_boundary", etc.
    content: Optional[str]  # For system messages
    level: Optional[str]  # Log level

    # Context compaction fields
    logicalParentUuid: Optional[str]  # Points to pre-compaction message
    compactMetadata: Optional[ClaudeCodeCompactMetadata]

    # Summary fields
    summary: Optional[str]
    summarizes: Optional[List[str]]
    summarizedMessages: Optional[List[str]]

    # File snapshot fields
    files: Optional[List[Dict[str, Any]]]
    snapshot: Optional[Dict[str, Any]]

    # Session metadata
    cwd: Optional[str]  # Working directory
    gitBranch: Optional[str]
    version: Optional[str]  # Claude Code version
    requestId: Optional[str]

    # Branching
    isSidechain: Optional[bool]

    # Thinking metadata
    thinkingMetadata: Optional[Dict[str, Any]]

    # Todo tracking
    todos: Optional[List[Dict[str, Any]]]


class ClaudeCodeSession(TypedDict, total=False):
    """A parsed Claude Code session."""
    session_id: str
    path: Optional[str]
    lines: List[ClaudeCodeLine]
    parse_errors: Optional[List[Dict[str, Any]]]
    is_subagent: Optional[bool]
    parent_session_id: Optional[str]


class ClaudeCodeExport(TypedDict, total=False):
    """Claude Code export bundle (multiple sessions)."""
    provider: Literal["claude-code"]
    sessions: List[ClaudeCodeSession]
    export_metadata: Optional[Dict[str, Any]]
