"""
Referential integrity validator.

Validates that parent references resolve to existing messages.
"""

from typing import List, Set

from ..base import NormalizedMessage
from .base import BaseValidator, ValidationContext, ValidationSeverity

# Known root sentinel values that aren't real missing parents
ROOT_SENTINELS = {"", "root", "aaa", "00000000-0000-0000-0000-000000000000"}


class ReferentialIntegrityValidator(BaseValidator):
    """Validates parent message references.

    Missing parent references are common and expected in:
    - ChatGPT: Branching exports may only include active path
    - Claude Code: Context summarization removes old messages
      but keeps parentUuid references

    These emit warnings, not errors, since data is still usable.
    """

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate parent references resolve."""
        if not self.config.has_parent_references:
            # Provider doesn't use parent references
            return

        # Build set of all message IDs
        message_ids: Set[str] = set()
        for msg in messages:
            msg_id = msg.get("provider_message_id")
            if msg_id:
                message_ids.add(msg_id)

        # Count orphaned references
        orphan_count = 0
        for msg in messages:
            parent_id = msg.get("provider_parent_id")
            if parent_id and parent_id not in message_ids:
                # Skip root sentinels
                if parent_id not in ROOT_SENTINELS:
                    orphan_count += 1

        if orphan_count > 0:
            self._add_issue(
                context,
                f"Conversation {context.conversation_id}: {orphan_count} message(s) reference "
                f"missing parents (expected for branched/summarized conversations)",
                ValidationSeverity.warning,
            )
