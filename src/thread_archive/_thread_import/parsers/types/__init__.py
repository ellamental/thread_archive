"""
Type definitions for provider-specific export formats.

Each module defines TypedDict schemas for the raw input format from that provider.
These are used for type checking and IDE support when parsing exports.
"""

from .chatgpt import (
    ChatGPTAuthor,
    ChatGPTContent,
    ChatGPTConversation,
    ChatGPTExport,
    ChatGPTMessage,
    ChatGPTNode,
)
from .claude import (
    ClaudeContentBlock,
    ClaudeConversation,
    ClaudeExport,
    ClaudeMessage,
)
from .claude_code import (
    ClaudeCodeExport,
    ClaudeCodeLine,
    ClaudeCodeMessage,
    ClaudeCodeSession,
)

__all__ = [
    # ChatGPT
    "ChatGPTAuthor",
    "ChatGPTContent",
    "ChatGPTMessage",
    "ChatGPTNode",
    "ChatGPTConversation",
    "ChatGPTExport",
    # Claude
    "ClaudeContentBlock",
    "ClaudeMessage",
    "ClaudeConversation",
    "ClaudeExport",
    # Claude Code
    "ClaudeCodeMessage",
    "ClaudeCodeLine",
    "ClaudeCodeSession",
    "ClaudeCodeExport",
]
