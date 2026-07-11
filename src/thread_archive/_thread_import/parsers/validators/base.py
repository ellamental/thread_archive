"""
Base validation infrastructure.

Provides ValidationContext and BaseValidator for building validators.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from ..base import NormalizedMessage
from ..config import ProviderConfig


class ValidationSeverity(str, Enum):
    """Severity level for validation issues."""
    error = "error"
    warning = "warning"


@dataclass
class ValidationContext:
    """Context for validation across a conversation.

    Tracks state as messages are processed and collects errors/warnings.
    """

    conversation_id: str
    source_provider: str
    strict: bool = False  # If True, warnings become errors

    # Counters
    message_count: int = 0
    first_user_msg_seen: bool = False
    first_user_msg_has_context: bool = False
    assistant_msg_count: int = 0
    assistant_msgs_with_thinking: int = 0
    assistant_msgs_with_tools: int = 0

    # Model-specific tracking
    thinking_required_msgs: int = 0
    thinking_required_msgs_with_thinking: int = 0

    # Results
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        """Add a validation error."""
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        """Add a validation warning."""
        if self.strict:
            self.errors.append(msg)
        else:
            self.warnings.append(msg)

    def add_issue(self, msg: str, severity: ValidationSeverity) -> None:
        """Add an issue with specified severity."""
        if severity == ValidationSeverity.error:
            self.add_error(msg)
        else:
            self.add_warning(msg)

    @property
    def has_errors(self) -> bool:
        """Check if any errors were recorded."""
        return len(self.errors) > 0


class BaseValidator:
    """Base class for validators with common utilities.

    Subclasses should override validate() and optionally
    validate_conversation_end().
    """

    def __init__(self, config: ProviderConfig, strict: bool = False):
        """Initialize validator.

        Args:
            config: Provider-specific configuration
            strict: If True, treat warnings as errors
        """
        self.config = config
        self.strict = strict

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate messages and update context.

        Override in subclasses to implement validation logic.

        Args:
            messages: Messages to validate
            context: Validation context to update
        """
        pass

    def validate_message(
        self, msg: NormalizedMessage, context: ValidationContext
    ) -> None:
        """Validate a single message.

        Override in subclasses for per-message validation.

        Args:
            msg: Message to validate
            context: Validation context to update
        """
        pass

    def validate_conversation_end(self, context: ValidationContext) -> None:
        """Perform end-of-conversation validation.

        Override in subclasses for aggregate validation.

        Args:
            context: Validation context to update
        """
        pass

    def _add_issue(
        self, context: ValidationContext, msg: str, severity: ValidationSeverity
    ) -> None:
        """Add an issue, respecting strict mode."""
        if self.strict and severity == ValidationSeverity.warning:
            severity = ValidationSeverity.error
        context.add_issue(msg, severity)

    @staticmethod
    def _has_thinking_blocks(msg: NormalizedMessage) -> bool:
        """Check if message has thinking/reasoning blocks."""
        blocks = msg.get("content_blocks", [])
        return any(b.get("type") == "thinking" for b in blocks)

    @staticmethod
    def _has_tool_blocks(msg: NormalizedMessage) -> bool:
        """Check if message has tool use or result blocks."""
        blocks = msg.get("content_blocks", [])
        return any(b.get("type") in ("tool_use", "tool_result") for b in blocks)

    @staticmethod
    def _has_user_context(msg: NormalizedMessage) -> bool:
        """Check if message has user context blocks."""
        blocks = msg.get("content_blocks", [])
        if any(b.get("type") == "system_context" for b in blocks):
            return True
        # Check provider_data for context indicators
        provider_data = msg.get("provider_data", {})
        raw_msg = provider_data.get("message", {})
        metadata = raw_msg.get("metadata", {})
        return bool(metadata.get("is_user_system_message"))

    @staticmethod
    def _get_model(msg: NormalizedMessage) -> Optional[str]:
        """Extract model name from message."""
        provider_data = msg.get("provider_data", {})
        # ChatGPT stores in message.metadata.model_slug
        raw_msg = provider_data.get("message", {})
        metadata = raw_msg.get("metadata", {})
        model = metadata.get("model_slug") or metadata.get("model")
        if model:
            return model
        # Check top-level provider_data
        return provider_data.get("model")
