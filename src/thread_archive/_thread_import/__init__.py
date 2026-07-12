"""
Thread Import Package

Private to thread_archive (hence the underscore): a vendored, dependency-free
parsing island. Not part of the package's public API until deliberately exposed.

Parsing library for importing conversations from AI services into Thread:
- Claude Code: Direct from ~/.claude/projects/*.jsonl
- Cursor: Direct from state.vscdb
- ChatGPT/Claude Web: From export ZIPs

Architecture:
- Parsers: Convert provider-specific formats to NormalizedMessage
- EventBuilder: Converts NormalizedMessage to Thread events
"""

from .event_builder import DefaultEventBuilder, EventBuilder, ThreadEvent
from .parsers import ChatGPTParser, ClaudeCodeParser, ClaudeParser, ProviderParser, get_parser

__all__ = [
    # Event Building
    "EventBuilder",
    "DefaultEventBuilder",
    "ThreadEvent",
    # Parsers
    "ChatGPTParser",
    "ClaudeParser",
    "ClaudeCodeParser",
    "ProviderParser",
    "get_parser",
]

