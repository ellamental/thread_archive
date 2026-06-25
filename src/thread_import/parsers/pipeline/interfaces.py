"""
Protocol definitions for pipeline components.

These Protocols define the interfaces for pluggable pipeline components.
Each component can be implemented independently and composed together.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, TypeVar

# Import from parent to avoid circular imports
# These will be available once the module structure is in place
from ..base import ContentBlock, NormalizedMessage

# Type variable for generic input type. Contravariant: the Parser Protocol only
# consumes it (parse(data: T_Input)), never produces it.
T_Input = TypeVar("T_Input", contravariant=True)


@dataclass
class RawMessage:
    """Intermediate representation between parse and normalize.

    This is the provider-specific parsed data before normalization.
    It preserves all provider-specific fields while providing a
    common structure for transformers to operate on.

    Attributes:
        provider_message_id: Original message ID from provider
        provider_conversation_id: Original conversation ID
        provider_parent_id: Parent message ID (for tree structures)
        role: Raw role value (not yet normalized)
        content: Raw content (provider-specific structure)
        content_type: Content type indicator (if applicable)
        timestamp: Raw timestamp (provider-specific format)
        is_active_path: Whether on main conversation thread
        is_stub: Whether this is a placeholder node
        metadata: Additional provider-specific metadata
        raw_data: Complete original data for reference
        children: IDs of child messages (for tree traversal)
    """

    provider_message_id: str
    provider_conversation_id: str
    provider_parent_id: Optional[str] = None
    role: Optional[str] = None
    content: Any = None
    content_type: Optional[str] = None
    timestamp: Any = None  # Provider-specific format
    timestamp_iso: Optional[str] = None  # Normalized ISO format
    message_order: Optional[int] = None
    is_active_path: bool = True
    is_stub: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)
    children: List[str] = field(default_factory=list)

    # Content blocks (built during parsing or transformation)
    content_blocks: List[ContentBlock] = field(default_factory=list)

    # Conversation-level metadata
    conversation_title: Optional[str] = None
    conversation_metadata: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        """Allow use in sets (based on message ID)."""
        return hash(self.provider_message_id)

    def __eq__(self, other: object) -> bool:
        """Equality based on message ID."""
        if not isinstance(other, RawMessage):
            return False
        return self.provider_message_id == other.provider_message_id


class Parser(Protocol[T_Input]):
    """Protocol for parsing raw provider data.

    Parsers convert provider-specific export data into RawMessage objects.
    This is the first phase of the pipeline.

    Type Parameters:
        T_Input: The type of raw input data (e.g., ChatGPTExport)
    """

    def parse(self, data: T_Input) -> List[RawMessage]:
        """Parse raw provider data into intermediate representation.

        Args:
            data: Raw provider export data

        Returns:
            List of RawMessage objects
        """
        ...


class Transformer(Protocol):
    """Protocol for transforming messages.

    Transformers modify the list of RawMessages. Common uses:
    - Coalescing tool calls into parent messages (ChatGPT)
    - Merging thinking blocks into parent (Claude Code)
    - Computing active path (ChatGPT)
    - Extracting IDE context (Claude Code)

    Transformers are applied in sequence, so order matters.
    """

    def transform(self, messages: List[RawMessage]) -> List[RawMessage]:
        """Transform a list of messages.

        Args:
            messages: Input messages

        Returns:
            Transformed messages (may be modified in place or new list)
        """
        ...


class Normalizer(Protocol):
    """Protocol for normalizing messages to canonical format.

    Normalizers convert RawMessage objects into NormalizedMessage
    dictionaries that match the unified schema.
    """

    def normalize(self, raw: RawMessage) -> NormalizedMessage:
        """Convert a RawMessage to NormalizedMessage.

        Args:
            raw: Intermediate message representation

        Returns:
            Canonical NormalizedMessage dictionary
        """
        ...


@dataclass
class ValidationContext:
    """Context for validation across a conversation.

    This replaces the ValidationContext in base.py with a cleaner
    interface that validators can update.
    """

    conversation_id: str
    source_provider: str
    message_count: int = 0
    first_user_msg_seen: bool = False
    first_user_msg_has_context: bool = False
    assistant_msg_count: int = 0
    assistant_msgs_with_thinking: int = 0
    assistant_msgs_with_tools: int = 0
    thinking_required_msgs: int = 0
    thinking_required_msgs_with_thinking: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """Add a validation error."""
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        """Add a validation warning."""
        self.warnings.append(msg)

    @property
    def has_errors(self) -> bool:
        """Check if any errors were recorded."""
        return len(self.errors) > 0


class Validator(Protocol):
    """Protocol for validating messages.

    Validators check messages against rules and update the
    ValidationContext with errors/warnings.
    """

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate messages and update context.

        Args:
            messages: Messages to validate
            context: Validation context to update with results
        """
        ...
