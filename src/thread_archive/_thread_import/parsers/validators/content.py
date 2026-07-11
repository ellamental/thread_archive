"""
Content validator.

Validates message content presence and timestamps.
"""

from typing import List

from ..base import NormalizedMessage
from .base import BaseValidator, ValidationContext, ValidationSeverity


class ContentValidator(BaseValidator):
    """Validates message content and metadata.

    Checks:
    - Messages have content_text or content_blocks
    - Messages have timestamps
    - First user message tracking (for context validation)

    Empty messages that aren't stubs are flagged as warnings.
    """

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate content across all messages."""
        for msg in messages:
            self._validate_message(msg, context)

        # End-of-conversation check
        self._validate_conversation_end(context)

    def _validate_message(
        self, msg: NormalizedMessage, context: ValidationContext
    ) -> None:
        """Validate a single message."""
        context.message_count += 1
        role = msg.get("role", "")

        # Track first user message
        if role == "user" and not context.first_user_msg_seen:
            context.first_user_msg_seen = True
            context.first_user_msg_has_context = self._has_user_context(msg)

        # Check for content
        has_text = bool(msg.get("content_text"))
        has_blocks = bool(msg.get("content_blocks"))

        if not has_text and not has_blocks:
            # Skip validation for stub messages (ChatGPT branching artifacts)
            provider_data = msg.get("provider_data", {})
            is_stub = provider_data.get("synthetic_stub") or not provider_data.get(
                "message"
            )
            if not is_stub:
                self._add_issue(
                    context,
                    f"Message {msg.get('provider_message_id', 'unknown')} "
                    f"has no content_text or content_blocks",
                    ValidationSeverity.warning,
                )

        # Check for timestamp
        if not msg.get("created_at"):
            self._add_issue(
                context,
                f"Message {msg.get('provider_message_id', 'unknown')} lacks created_at timestamp",
                ValidationSeverity.warning,
            )

    def _validate_conversation_end(self, context: ValidationContext) -> None:
        """Perform end-of-conversation validation."""
        # Check if we saw any user messages
        if not context.first_user_msg_seen and context.message_count > 0:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id} has no user messages",
                ValidationSeverity.warning,
            )
