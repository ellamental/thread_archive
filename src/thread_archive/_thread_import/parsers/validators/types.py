"""
Type validator.

Validates roles and content block types against known values.
"""

from typing import List, Set

from ..base import NormalizedMessage
from .base import BaseValidator, ValidationContext, ValidationSeverity

# Universal known block types across all providers
KNOWN_BLOCK_TYPES: Set[str] = {
    "text",
    "thinking",
    "tool_use",
    "tool_result",
    "image",
    "file",
    "code",
    "system_context",
    "parse_error",
    "file_snapshot",
    "context_summary",
    "system_metadata",
    "ide_context",
    "queue_operation",
    "progress",
}


class TypeValidator(BaseValidator):
    """Validates message roles and content block types.

    Unknown types get warnings (not errors) because:
    - Provider may have added new features
    - We want to preserve unknown data, not reject it

    This helps detect format changes in provider exports.
    """

    def validate(
        self, messages: List[NormalizedMessage], context: ValidationContext
    ) -> None:
        """Validate types across all messages."""
        unknown_roles: Set[str] = set()
        unknown_block_types: Set[str] = set()

        for msg in messages:
            role = msg.get("role", "")

            # Check role against provider's expected roles
            if role and self.config.expected_roles:
                if role not in self.config.expected_roles:
                    unknown_roles.add(role)

            # Check content block types
            for block in msg.get("content_blocks", []):
                block_type = block.get("type", "")
                if block_type:
                    # Check against universal known types
                    if block_type not in KNOWN_BLOCK_TYPES:
                        unknown_block_types.add(block_type)
                    # Optionally check against provider-specific expected types
                    elif (
                        self.config.expected_block_types
                        and block_type not in self.config.expected_block_types
                    ):
                        # Provider has restricted set - warn about unexpected
                        unknown_block_types.add(block_type)

        # Report unknown types
        for role in sorted(unknown_roles):
            self._add_issue(
                context,
                f"Unknown message role '{role}' from provider {self.config.provider_name} - "
                f"possible format change or new feature",
                ValidationSeverity.warning,
            )

        for block_type in sorted(unknown_block_types):
            self._add_issue(
                context,
                f"Unknown content block type '{block_type}' - "
                f"possible format change or new feature",
                ValidationSeverity.warning,
            )
