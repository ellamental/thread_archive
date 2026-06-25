"""
Thinking block validator.

Validates thinking block presence based on provider expectations
and model-specific requirements.
"""

from typing import List

from ..base import NormalizedMessage
from .base import BaseValidator, ValidationContext, ValidationSeverity


class ThinkingBlockValidator(BaseValidator):
    """Validates thinking block presence.

    Uses ProviderConfig to determine expectations:
    - "never": Error if thinking blocks exist (wrong parser/source)
    - "required": Error if no thinking blocks (incomplete export)
    - "model_specific": Validate per-message based on model
    - "optional": No validation

    This is a critical validator for catching:
    1. Data parsed with wrong provider
    2. Incomplete exports missing thinking data
    """

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate thinking blocks across all messages."""
        # First pass: count and track per-message
        for msg in messages:
            self._track_message(msg, context)

        # Second pass: aggregate validation
        self._validate_aggregate(context)

    def _track_message(
        self, msg: NormalizedMessage, context: ValidationContext
    ) -> None:
        """Track thinking block presence for a message."""
        role = msg.get("role", "")
        if role != "assistant":
            return

        context.assistant_msg_count += 1
        has_thinking = self._has_thinking_blocks(msg)

        if has_thinking:
            context.assistant_msgs_with_thinking += 1

        # Track model-specific requirements
        if self.config.thinking_expectation == "model_specific":
            model = self._get_model(msg)
            if self.config.requires_thinking(model):
                context.thinking_required_msgs += 1
                if has_thinking:
                    context.thinking_required_msgs_with_thinking += 1

    def _validate_aggregate(self, context: ValidationContext) -> None:
        """Perform aggregate validation at conversation end."""
        if context.assistant_msg_count == 0:
            return

        thinking_pct = (
            context.assistant_msgs_with_thinking / context.assistant_msg_count * 100
        )

        expectation = self.config.thinking_expectation

        if expectation == "never":
            self._validate_never(context, thinking_pct)
        elif expectation == "required":
            self._validate_required(context, thinking_pct)
        elif expectation == "model_specific":
            self._validate_model_specific(context)

    @staticmethod
    def _is_subagent(context: ValidationContext) -> bool:
        """Subagent sessions (agent-*) use Haiku without extended thinking."""
        return bool(
            context.conversation_id
            and context.conversation_id.startswith("agent-")
        )

    def _validate_never(
        self, context: ValidationContext, thinking_pct: float
    ) -> None:
        """``never``: any thinking blocks signal the wrong parser/source."""
        if thinking_pct > 0:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: {self.config.provider_name} exports "
                f"should NEVER have thinking blocks (found {thinking_pct:.0f}%). "
                f"Wrong parser or data source?",
                ValidationSeverity.error,
            )

    def _validate_required(
        self, context: ValidationContext, thinking_pct: float
    ) -> None:
        """``required``: every (non-subagent) assistant turn must have thinking."""
        if self._is_subagent(context):
            return
        if thinking_pct == 0:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: {self.config.provider_name} exports "
                f"MUST have thinking blocks (found 0%). Incomplete export.",
                ValidationSeverity.error,
            )
        elif thinking_pct < 100:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: {thinking_pct:.0f}% of "
                f"assistant messages have thinking blocks (expected 100%)",
                ValidationSeverity.warning,
            )

    def _validate_model_specific(self, context: ValidationContext) -> None:
        """``model_specific``: only thinking-capable model turns are checked."""
        if self._is_subagent(context) or context.thinking_required_msgs <= 0:
            return
        model_thinking_pct = (
            context.thinking_required_msgs_with_thinking
            / context.thinking_required_msgs
            * 100
        )
        if model_thinking_pct == 0:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: thinking-capable model messages "
                f"should have thinking blocks (found 0%). Incomplete export.",
                ValidationSeverity.error,
            )
        elif model_thinking_pct < 100:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: {model_thinking_pct:.0f}% of "
                f"thinking-capable model messages have thinking blocks",
                ValidationSeverity.warning,
            )
