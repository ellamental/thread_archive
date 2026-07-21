"""
Base class for provider-specific chat export parsers.

Defines the unified ContentBlock schema used across all providers:
- ChatGPT: Tool calls, thinking, and results are coalesced into parent message blocks
- Claude: Content blocks are normalized to the common schema

Also provides explicit field mapping infrastructure for semantic correctness:
- FieldMapping: Declares how provider fields map to our model

Validation is handled by pluggable validators in validators/.
"""

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict, Union

# =============================================================================
# Content Block Schema Version
# =============================================================================
CURRENT_CONTENT_BLOCKS_VERSION = 1


# =============================================================================
# Content Block Types - Unified schema for all providers
# =============================================================================

class TextBlock(TypedDict, total=False):
    """Plain text content block."""
    type: Literal["text"]
    text: str
    seq: int
    # Optional timing (Claude)
    start_timestamp: Optional[str]
    stop_timestamp: Optional[str]
    # Optional provider-specific ID (for annotation targeting)
    provider_block_id: Optional[str]


class ThinkingBlock(TypedDict, total=False):
    """Model thinking/reasoning block."""
    type: Literal["thinking"]
    text: str
    seq: int
    # ChatGPT content_type variants: thoughts, analysis, reasoning_recap
    thinking_type: Optional[str]
    provider_block_id: Optional[str]


class ToolUseBlock(TypedDict, total=False):
    """Tool/function call block."""
    type: Literal["tool_use"]
    name: str  # Canonical tool name (Edit, Write, Read, Bash, etc.)
    provider_tool_name: Optional[str]  # Original provider name (edit_file_v2, etc.)
    tool_call_id: Optional[str]  # Unique ID for this tool call (links to tool_result)
    input: Any  # Tool arguments
    seq: int
    # Optional display content (Claude)
    message: Optional[str]
    display_content: Optional[Any]
    start_timestamp: Optional[str]
    stop_timestamp: Optional[str]
    # If this came from a separate ChatGPT message, store its ID
    provider_message_id: Optional[str]
    provider_block_id: Optional[str]


class ToolResultBlock(TypedDict, total=False):
    """Tool/function result block."""
    type: Literal["tool_result"]
    tool_use_id: Optional[str]  # Links to corresponding tool_use block's tool_call_id
    name: str
    content: Any  # Result content
    is_error: bool
    seq: int
    display_content: Optional[Any]
    # If this came from a separate ChatGPT message, store its ID
    provider_message_id: Optional[str]
    provider_block_id: Optional[str]


class ImageBlock(TypedDict, total=False):
    """Image content block."""
    type: Literal["image"]
    url: Optional[str]
    asset_pointer: Optional[str]  # ChatGPT asset reference
    mime_type: Optional[str]
    seq: int
    provider_block_id: Optional[str]


class FileBlock(TypedDict, total=False):
    """File attachment block."""
    type: Literal["file"]
    name: str
    url: Optional[str]
    mime_type: Optional[str]
    size: Optional[int]
    seq: int
    provider_block_id: Optional[str]


class CodeBlock(TypedDict, total=False):
    """Code execution block."""
    type: Literal["code"]
    language: Optional[str]
    code: str
    seq: int
    provider_block_id: Optional[str]


class SystemContextBlock(TypedDict, total=False):
    """System/user context block (custom instructions, etc.)."""
    type: Literal["system_context"]
    text: str
    context_type: Optional[str]  # user_editable_context, model_editable_context, etc.
    seq: int
    provider_block_id: Optional[str]


class ParseErrorBlock(TypedDict, total=False):
    """Preserved malformed data with error context.

    Instead of silently dropping data that fails to parse, we store it here.
    This ensures nothing is lost and makes parse failures visible.
    """
    type: Literal["parse_error"]
    raw_text: str  # The original text that failed to parse
    error: str  # The error message
    line_number: Optional[int]  # Line number in source file, if applicable
    seq: int
    provider_block_id: Optional[str]


class FileSnapshotBlock(TypedDict, total=False):
    """File state snapshot (Claude Code file-history-snapshot).

    Captures the state of files at a point in the conversation.
    Used by Claude Code to track file changes during a session.
    """
    type: Literal["file_snapshot"]
    files: List[Dict[str, Any]]  # List of file states
    timestamp: Optional[str]
    seq: int
    provider_block_id: Optional[str]


class ContextSummaryBlock(TypedDict, total=False):
    """Context continuation summary (Claude Code summary type).

    When a conversation runs out of context, Claude creates a summary
    of prior messages. This block preserves that summary and tracks
    which messages it replaced.
    """
    type: Literal["context_summary"]
    text: str  # The summary text
    summarizes: Optional[List[str]]  # Message IDs that were summarized
    seq: int
    provider_block_id: Optional[str]


class SystemMetadataBlock(TypedDict, total=False):
    """Internal system metadata (token_budget, etc.).

    Preserves provider-specific internal data that doesn't fit
    other block types but shouldn't be dropped.
    """
    type: Literal["system_metadata"]
    metadata_type: str  # e.g., "token_budget"
    data: Dict[str, Any]  # The actual metadata
    seq: int
    provider_block_id: Optional[str]


# Union of all block types
ContentBlock = Union[
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    ImageBlock,
    FileBlock,
    CodeBlock,
    SystemContextBlock,
    ParseErrorBlock,
    FileSnapshotBlock,
    ContextSummaryBlock,
    SystemMetadataBlock,
]


# =============================================================================
# Field Mapping Infrastructure
# =============================================================================


class ValidationSeverity(str, Enum):
    """Severity level for validation issues."""

    error = "error"  # Fatal - import should abort
    warning = "warning"  # Non-fatal - import continues with warning


@dataclass
class FieldMapping:
    """
    Explicit mapping from provider field(s) to our model field.

    This ensures we're intentional about semantic meaning, not just
    matching field names. Each mapping documents what the field MEANS
    in our model, not just where it comes from.

    Examples:
        # Simple field rename
        FieldMapping(
            model_field="created_at",
            extractor=lambda msg: msg.get("create_time"),
            semantic_doc="When the message was originally sent (immutable)"
        )

        # Complex extraction from nested structure
        FieldMapping(
            model_field="content_text",
            extractor=extract_content_text,  # dedicated function
            semantic_doc="Primary display text for the message"
        )

        # Computed from multiple fields
        FieldMapping(
            model_field="message_order",
            extractor=lambda msg: int(msg.get("timestamp", 0) * 1_000_000),
            semantic_doc="Microsecond-precision ordering within conversation"
        )
    """

    model_field: str
    extractor: Callable[[Dict[str, Any]], Any]
    required: bool = True
    default: Any = None
    semantic_doc: str = ""

    def extract(self, raw_data: Dict[str, Any]) -> Any:
        """Extract the field value from raw provider data."""
        try:
            value = self.extractor(raw_data)
            if value is None and self.default is not None:
                return self.default
            return value
        except Exception:
            if self.required:
                raise
            return self.default


# =============================================================================
# Normalized Message Schema
# =============================================================================

class NormalizedMessage(TypedDict, total=False):
    """
    Unified message format output by all parsers.

    This is the canonical format stored in the database.
    """
    # Core identifiers
    source_provider: str
    provider_message_id: str
    provider_conversation_id: str
    content_hash: str

    # Normalized fields
    role: str  # 'user', 'assistant', 'system'
    content_text: str  # Primary display text (extracted from blocks)
    content_blocks: List[ContentBlock]  # Structured content
    is_visually_hidden: bool  # Display hint — system/snapshot/meta rows hidden by default

    # Timestamps
    created_at: Optional[str]  # ISO format
    updated_at: Optional[str]  # ISO format
    message_order: Optional[int]  # Ordering within conversation

    # Provider data preservation (exact original structure)
    provider_data: Dict[str, Any]

    # ChatGPT-specific (for branching)
    provider_parent_id: Optional[str]
    provider_parent_id_lower: Optional[str]
    provider_message_id_lower: Optional[str]
    is_active_path: Optional[bool]

    # Conversation metadata
    conversation_title: Optional[str]
    conversation_metadata: Optional[Dict[str, Any]]


class ProviderParser(ABC):
    """
    Base class for provider-specific parsers.

    Subclasses should define:
    - PROVIDER_NAME: str - Identifier for this provider
    - FIELD_MAPPINGS: List[FieldMapping] - Explicit field mappings (optional, for new-style parsers)

    And implement:
    - parse_export() - Parse provider data into normalized messages
    """

    # Override in subclasses
    PROVIDER_NAME: str = "unknown"
    FIELD_MAPPINGS: List[FieldMapping] = []

    def __init__(self, strict: bool = False):
        """
        Initialize the parser.

        Args:
            strict: If True, validation warnings become errors
        """
        self.strict = strict

    @abstractmethod
    def parse_export(self, data: Any) -> List[NormalizedMessage]:
        """
        Parse provider export data into normalized message format.

        Returns list of NormalizedMessage dicts with:
        - source_provider: str - Provider identifier (chatgpt, claude, cursor)
        - provider_message_id: str - Original message ID from provider
        - provider_conversation_id: str - Original conversation ID
        - content_hash: str - Deterministic hash for deduplication
        - role: str - Normalized role (user, assistant, system)
        - content_text: str - Primary display text
        - content_blocks: List[ContentBlock] - Structured content blocks
        - provider_data: dict - Exact original data for reconstruction
        - created_at/updated_at: str (ISO format)
        - message_order: int - Ordering within conversation
        """
        pass

    def apply_field_mappings(
        self, raw_data: Dict[str, Any], mappings: Optional[List[FieldMapping]] = None
    ) -> Dict[str, Any]:
        """
        Apply field mappings to extract values from raw provider data.

        Args:
            raw_data: Raw provider data dictionary
            mappings: List of FieldMapping to apply (uses self.FIELD_MAPPINGS if None)

        Returns:
            Dictionary with model field names as keys and extracted values
        """
        if mappings is None:
            mappings = self.FIELD_MAPPINGS

        result: Dict[str, Any] = {}
        for mapping in mappings:
            try:
                value = mapping.extract(raw_data)
                result[mapping.model_field] = value
            except Exception as e:
                if mapping.required:
                    raise ValueError(
                        f"Failed to extract required field '{mapping.model_field}': {e}"
                    )
                result[mapping.model_field] = mapping.default

        return result

    @staticmethod
    def hash_message(msg_id: str, role: str, content: Any, create_time: Any) -> str:
        """Create deterministic hash for deduplication."""
        # Normalize create_time so equivalent numeric values hash the same.
        # Chat export files sometimes represent timestamps as ints or floats
        # (e.g. 1700000000 vs 1700000000.0).
        normalized_create_time = create_time
        if isinstance(create_time, float):
            if math.isfinite(create_time) and create_time.is_integer():
                normalized_create_time = int(create_time)
        hash_input = json.dumps(
            {
                "id": msg_id,
                "role": role,
                "content": content,
                "create_time": normalized_create_time,
            },
            sort_keys=True,
        )
        return hashlib.sha256(hash_input.encode()).hexdigest()

    @staticmethod
    def extract_text_from_blocks(blocks: List[ContentBlock]) -> str:
        """Extract primary display text from content blocks."""
        text_parts: List[str] = []
        for block in blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block_type == "system_context":
                # Include system context in display text
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        return "\n\n".join(text_parts).strip()

    @staticmethod
    def normalize_role(role: Optional[str], sender: Optional[str] = None) -> str:
        """Normalize role/sender to standard values."""
        if role:
            role_lower = role.lower()
            if role_lower in ("user", "human"):
                return "user"
            elif role_lower == "assistant":
                return "assistant"
            elif role_lower in ("system", "tool"):
                return role_lower
        if sender:
            sender_lower = sender.lower()
            if sender_lower == "human":
                return "user"
            elif sender_lower == "assistant":
                return "assistant"
        return role or "unknown"

    @staticmethod
    def create_text_block(text: str, seq: int, **kwargs) -> TextBlock:
        """Create a text content block."""
        block: TextBlock = {"type": "text", "text": text, "seq": seq}
        for key, value in kwargs.items():
            if value is not None:
                block[key] = value  # type: ignore
        return block

    @staticmethod
    def create_tool_use_block(
        name: str,
        input_data: Any,
        seq: int,
        provider_message_id: Optional[str] = None,
        **kwargs
    ) -> ToolUseBlock:
        """Create a tool use content block."""
        block: ToolUseBlock = {
            "type": "tool_use",
            "name": name,
            "input": input_data,
            "seq": seq,
        }
        if provider_message_id:
            block["provider_message_id"] = provider_message_id
        for key, value in kwargs.items():
            if value is not None:
                block[key] = value  # type: ignore
        return block

    @staticmethod
    def create_tool_result_block(
        name: str,
        content: Any,
        seq: int,
        is_error: bool = False,
        provider_message_id: Optional[str] = None,
        **kwargs
    ) -> ToolResultBlock:
        """Create a tool result content block."""
        block: ToolResultBlock = {
            "type": "tool_result",
            "name": name,
            "content": content,
            "is_error": is_error,
            "seq": seq,
        }
        if provider_message_id:
            block["provider_message_id"] = provider_message_id
        for key, value in kwargs.items():
            if value is not None:
                block[key] = value  # type: ignore
        return block

    @staticmethod
    def create_thinking_block(
        text: str,
        seq: int,
        thinking_type: Optional[str] = None,
        **kwargs
    ) -> ThinkingBlock:
        """Create a thinking/reasoning content block."""
        block: ThinkingBlock = {"type": "thinking", "text": text, "seq": seq}
        if thinking_type:
            block["thinking_type"] = thinking_type
        for key, value in kwargs.items():
            if value is not None:
                block[key] = value  # type: ignore
        return block
