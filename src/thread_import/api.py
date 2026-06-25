"""Public API for thread_import.

This module defines the interfaces that consumers (like backend/importer.py)
should use to access archive data. The key principle is that consumers never
touch the archive database directly - they go through ImportSource.

This creates a clean boundary:
- Archive internals can change without breaking consumers
- Consumers can be tested with mock ImportSource implementations
- Coalescing and normalization happen in one place (the source)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional, Protocol

from .parsers.base import NormalizedMessage


@dataclass
class ConversationMeta:
    """Metadata about a conversation for import decisions.

    This is what ImportSource.list_conversations() yields.
    Contains enough info to decide whether to import without
    loading all messages.
    """
    source_provider: str
    provider_conversation_id: str
    title: Optional[str] = None
    message_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ImportSource(Protocol):
    """Protocol for anything that can produce messages for import.

    Implementations:
    - DirectParseSource: Parses export files directly
    - InMemorySource: For testing
    """

    def list_conversations(
        self,
        provider_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterator[ConversationMeta]:
        """List available conversations.

        Args:
            provider_filter: Only include this provider (e.g., "claude-code")
            limit: Maximum number of conversations to return

        Yields:
            ConversationMeta for each conversation
        """
        ...

    def get_messages(
        self,
        provider: str,
        conversation_id: str,
        coalesce: bool = True,
    ) -> Iterator[NormalizedMessage]:
        """Get messages for a conversation.

        Args:
            provider: Source provider name
            conversation_id: Provider's conversation ID
            coalesce: Whether to coalesce consecutive assistant messages

        Yields:
            NormalizedMessage objects in order, ready for event creation
        """
        ...
