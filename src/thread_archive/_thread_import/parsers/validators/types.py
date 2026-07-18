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
    "attachment",
    "model_change",
    "unknown_line",
    "flag",  # claude.ai safety marker blocks, preserved raw
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
        unmodeled_line_types: Set[str] = set()
        unknown_line_fields: Set[str] = set()
        unknown_message_fields: Set[str] = set()

        for msg in messages:
            role = msg.get("role", "")

            # Field-level drift: a NEW key on an already-modeled source line is
            # invisible to the type/role/line-type ledgers — it rides into
            # provider_data["line"] and is then dropped at the builder seam
            # unless modeled or annotated. Compare the raw line's keys (and its
            # nested message object's keys) against the provider's known sets.
            if self.config.known_line_fields:
                line = (msg.get("provider_data") or {}).get("line")
                if isinstance(line, dict):
                    known = self.config.known_line_fields.get(role)
                    if known:
                        unknown_line_fields.update(
                            f"{role}.{k}" for k in line if k not in known
                        )
                    if self.config.known_message_fields:
                        inner = line.get("message")
                        if isinstance(inner, dict):
                            unknown_message_fields.update(
                                k for k in inner
                                if k not in self.config.known_message_fields
                            )

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
                # An unknown_line block is the parser's verbatim preservation of
                # a line kind it doesn't model. Kinds the parser knowingly
                # preserves (config.expected_unmodeled_line_types) are fine; any
                # other kind means the provider grew a new line type — the
                # actual format-drift signal, named specifically.
                if block_type == "unknown_line":
                    line_type = block.get("line_type") or "unknown"
                    if line_type not in self.config.expected_unmodeled_line_types:
                        unmodeled_line_types.add(line_type)

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

        for line_type in sorted(unmodeled_line_types):
            self._add_issue(
                context,
                f"Unmodeled source line type '{line_type}' preserved verbatim - "
                f"possible format change or new feature",
                ValidationSeverity.warning,
            )

        for field_name in sorted(unknown_line_fields):
            self._add_issue(
                context,
                f"Unmodeled source line field '{field_name}' - a new field on a "
                f"known line type; its value is NOT persisted until modeled or "
                f"annotated (field-level format drift)",
                ValidationSeverity.warning,
            )

        for field_name in sorted(unknown_message_fields):
            self._add_issue(
                context,
                f"Unmodeled message field '{field_name}' - a new field on a known "
                f"message object; its value is NOT persisted until modeled or "
                f"annotated (field-level format drift)",
                ValidationSeverity.warning,
            )
