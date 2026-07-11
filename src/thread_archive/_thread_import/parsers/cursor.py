"""
Cursor IDE chat export parser.

Parses chat history exported from Cursor IDE.

## Format

Storage location (local machine, not devcontainers):

- **macOS**: ``~/Library/Application Support/Cursor/User/workspaceStorage/``
- **Linux**: ``~/.config/Cursor/User/workspaceStorage/``
- **Windows**: ``%APPDATA%\\Cursor\\User\\workspaceStorage\\``

Each workspace has a hashed subdirectory containing ``state.vscdb`` (SQLite).

Export methods:

1. ``cursor-export`` CLI tool (recommended) → ZIP with ``cursor_chats.json``
2. Cursor UI: Chat menu → Export Chat → Markdown file (single chat only)

## Edge Cases

Multiple Format Versions
    Cursor's internal format has evolved. The parser handles:
    - v1: Simple messages with ``content`` string
    - v2: Structured ``content_blocks`` with tool calls inline
    The exporter normalizes both to our schema.

Workspace Isolation
    Each workspace has separate chat history. Cross-workspace conversations
    don't exist. The ``workspace_hash`` field tracks origin.

Markdown Export Limitations
    Built-in Markdown export loses structure (tool calls become text).
    Use ``cursor-export`` CLI for full fidelity.

Timestamp Formats
    May be ISO string or Unix timestamp (seconds or milliseconds).
    Parser auto-detects and normalizes.

Role Normalization
    Some versions use ``"human"``/``"ai"`` instead of ``"user"``/``"assistant"``.
    Normalized automatically.

Tool Calls
    Cursor stores tool calls in dedicated ``tool_calls`` array on messages,
    separate from ``content``. Converted to ``tool_use`` content blocks.

## Validation

- **Thinking blocks**: Model-specific. See ``ProviderConfig`` for exempt models.
- **Parent references**: Not applicable (linear conversations).
- **Branching**: None.

## Field Mapping

- ``id`` → ``provider_message_id``
- ``conversation_id`` → ``provider_conversation_id``
- ``created_at`` → ``created_at`` (normalized to ISO)
- ``role`` → ``role`` (``"human"`` → ``"user"``, ``"ai"`` → ``"assistant"``)
- ``content`` / ``content_blocks`` → ``content_blocks``
"""

from typing import Any, Dict, List, Optional, cast

from thread_archive._thread_import.timestamps import parse_timestamp_iso

from . import cursor_blocks
from .base import (
    ContentBlock,
    FieldMapping,
    NormalizedMessage,
    ProviderParser,
    SemanticCheck,
    ValidationSeverity,
)
from .config import CURSOR_CONFIG, ProviderConfig
from .validators import (
    ContentValidator,
    ReferentialIntegrityValidator,
    ThinkingBlockValidator,
    TypeValidator,
)


def _parse_cursor_timestamp(ts: Any) -> Optional[str]:
    """Parse timestamp to ISO format, handling multiple formats."""
    return parse_timestamp_iso(ts)


def _extract_created_at(msg: Dict[str, Any]) -> Optional[str]:
    """Extract and normalize created_at timestamp."""
    return _parse_cursor_timestamp(msg.get("created_at"))


def _extract_role(msg: Dict[str, Any]) -> str:
    """Extract and normalize role."""
    role = msg.get("role", "")
    if not role:
        return "unknown"

    role_lower = role.lower()
    role_map = {
        "user": "user",
        "human": "user",
        "assistant": "assistant",
        "ai": "assistant",
        "bot": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    }
    return cast(str, role_map.get(role_lower, role_lower))


def _extract_content(msg: Dict[str, Any]) -> str:
    """Extract primary content as string."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    return ""


def _extract_model(msg: Dict[str, Any]) -> Optional[str]:
    """Extract model name if present."""
    return msg.get("model")


class CursorParser(ProviderParser):
    """Parser for Cursor IDE chat exports.

    Handles exports created by:
    1. cursor-export CLI tool (ZIP with cursor_chats.json)
    2. Cursor's built-in Markdown export (single chat)

    Outputs normalized messages with content_blocks in the unified schema.

    ## Explicit Field Mappings

    This parser uses explicit semantic mappings to ensure we correctly interpret
    Cursor's export format. Each mapping documents what the field MEANS in
    our model, not just where it comes from in the provider JSON.
    """

    PROVIDER_NAME = "cursor"

    # Provider configuration (replaces centralized PROVIDER_EXPECTATIONS)
    PROVIDER_CONFIG: ProviderConfig = CURSOR_CONFIG

    # Explicit field mappings with semantic documentation
    MESSAGE_FIELD_MAPPINGS = [
        FieldMapping(
            model_field="created_at",
            extractor=_extract_created_at,
            required=False,
            semantic_doc=(
                "When the message was SENT. Cursor may use ISO strings or Unix "
                "timestamps (seconds or milliseconds). Represents user-facing time."
            ),
        ),
        FieldMapping(
            model_field="role",
            extractor=_extract_role,
            required=True,
            semantic_doc=(
                "The actor role. Cursor uses various terms like 'user', 'human', "
                "'assistant', 'ai', 'bot'. We normalize to user/assistant/system/tool."
            ),
        ),
        FieldMapping(
            model_field="content",
            extractor=_extract_content,
            required=False,
            semantic_doc=(
                "Primary text content. May be empty if content is in content_blocks."
            ),
        ),
        FieldMapping(
            model_field="model",
            extractor=_extract_model,
            required=False,
            semantic_doc=(
                "The model that generated this message (for assistant messages). "
                "e.g., 'gpt-4', 'claude-3-opus', etc."
            ),
        ),
    ]

    # Semantic checks for Cursor-specific requirements
    SEMANTIC_CHECKS = [
        SemanticCheck(
            name="user_context_on_first_message",
            description=(
                "First user message should have user context. In Cursor, this is "
                "typically the workspace context, open files, and selection."
            ),
            applies_to="first_user_message",
            severity=ValidationSeverity.error,
        ),
        SemanticCheck(
            name="thinking_on_assistant_messages",
            description=(
                "Assistant messages may have thinking blocks depending on the model. "
                "Not all models support extended thinking."
            ),
            applies_to="assistant_messages",
            severity=ValidationSeverity.warning,
        ),
        SemanticCheck(
            name="tool_use_tracking",
            description=(
                "Cursor heavily uses tools (file read, grep, terminal, etc.). "
                "Tool calls and results should be captured in content_blocks."
            ),
            applies_to="assistant_messages",
            severity=ValidationSeverity.warning,
        ),
    ]

    def __init__(self, strict: bool = False):
        """Initialize the Cursor parser."""
        super().__init__(strict=strict)
        # Initialize validators from the new architecture
        self._validators = [
            ThinkingBlockValidator(self.PROVIDER_CONFIG, strict=strict),
            ReferentialIntegrityValidator(self.PROVIDER_CONFIG, strict=strict),
            TypeValidator(self.PROVIDER_CONFIG, strict=strict),
            ContentValidator(self.PROVIDER_CONFIG, strict=strict),
        ]

    def parse_export(self, data: Any) -> List[NormalizedMessage]:
        """
        Parse Cursor chat export data into normalized message format.

        Args:
            data: Either:
                - A dict with 'conversations' key (from cursor_chats.json)
                - A dict with 'provider': 'cursor' (full export bundle)
                - Raw conversations list
                - Markdown content string (from Cursor's built-in export)

        Returns:
            List of NormalizedMessage dictionaries with content_blocks
        """
        # Handle different input formats
        if isinstance(data, str):
            # Markdown export from Cursor's built-in feature
            return self._parse_markdown_export(data)

        if isinstance(data, dict):
            # Check for cursor_chats.json format
            if data.get("provider") == "cursor" or "conversations" in data:
                conversations = data.get("conversations", [])
            else:
                # Single conversation
                conversations = [data]
        elif isinstance(data, list):
            conversations = data
        else:
            return []

        messages: List[NormalizedMessage] = []

        for conv in conversations:
            conv_id = conv.get("id") or self._generate_id(conv)
            conv_title = conv.get("title") or "Untitled"
            workspace_name = conv.get("workspace_name") or ""
            workspace_uri = conv.get("workspace_uri") or ""

            # Extract conversation-level timestamps as fallback
            conv_created_at = conv.get("created_at")
            conv_updated_at = conv.get("updated_at")

            # Build conversation metadata
            conv_metadata = {
                "title": conv_title,
                "workspace_hash": conv.get("workspace_hash"),
                "workspace_name": workspace_name,
                "workspace_uri": workspace_uri,
            }

            conv_messages = conv.get("messages", [])

            for msg_idx, msg in enumerate(conv_messages):
                msg_record = self._parse_message(
                    msg,
                    conv_id,
                    conv_title,
                    conv_metadata,
                    msg_idx,
                    conv_created_at=conv_created_at,
                    conv_updated_at=conv_updated_at,
                )
                if msg_record:
                    messages.append(msg_record)

        return messages

    # parse_export_with_validation is inherited from ProviderParser (shared logic
    # driving self._validators); no provider-specific override needed.

    def _parse_message(
        self,
        msg: Dict[str, Any],
        conv_id: str,
        conv_title: str,
        conv_metadata: Dict[str, Any],
        msg_idx: int,
        conv_created_at: Any = None,
        conv_updated_at: Any = None,
    ) -> Optional[NormalizedMessage]:
        """Parse a single message from Cursor's format using explicit field mappings."""
        msg_id = msg.get("id") or f"{conv_id}_{msg_idx}"

        # Apply explicit field mappings for semantic correctness
        mapped_fields = self.apply_field_mappings(msg, self.MESSAGE_FIELD_MAPPINGS)
        role = mapped_fields.get("role", "unknown")
        content = mapped_fields.get("content", "")
        model = mapped_fields.get("model")
        created_at_iso = mapped_fields.get("created_at")

        raw_blocks = msg.get("content_blocks", [])

        # Build content blocks
        content_blocks = self._build_content_blocks(msg, raw_blocks, content)

        # Extract primary text
        content_text = self.extract_text_from_blocks(content_blocks)
        if not content_text and content:
            content_text = content if isinstance(content, str) else ""

        # Fallback to conversation-level timestamp if message has none
        # Semantic: We prefer message-level timestamp but use conversation timestamp
        # as a reasonable approximation when message timestamp is missing
        if not created_at_iso and conv_updated_at:
            created_at_iso = _parse_cursor_timestamp(conv_updated_at)
        if not created_at_iso and conv_created_at:
            created_at_iso = _parse_cursor_timestamp(conv_created_at)

        # Create the normalized message
        msg_record: NormalizedMessage = {
            "source_provider": self.PROVIDER_NAME,
            "provider_message_id": msg_id,
            "provider_message_id_lower": msg_id.lower() if msg_id else None,
            "provider_conversation_id": conv_id,
            "content_hash": self.hash_message(msg_id, role, content, msg.get("created_at")),
            "role": role,
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": created_at_iso,
            "updated_at": None,
            "message_order": msg_idx,
            # Provider data preservation
            "provider_data": {
                "message": msg,
                "conversation_title": conv_title,
                "model": model,
            },
            # Cursor doesn't have branching like ChatGPT
            "provider_parent_id": None,
            "provider_parent_id_lower": None,
            "is_active_path": True,
            # Conversation metadata
            "conversation_title": conv_title,
            "conversation_metadata": conv_metadata,
        }

        return msg_record

    def _build_content_blocks(
        self,
        msg: Dict[str, Any],
        raw_blocks: List[Dict],
        fallback_content: Any,
    ) -> List[ContentBlock]:
        """Build normalized content blocks from Cursor message data.

        Delegates to :func:`cursor_blocks.build_content_blocks`; the heavy,
        stateless per-shape appenders live in that sibling module. Kept on the
        class because tests call it directly.
        """
        return cursor_blocks.build_content_blocks(msg, raw_blocks, fallback_content)

    def _normalize_tool_input(
        self,
        original_name: str,
        canonical_name: str,
        input_data: Any,
        msg: Dict[str, Any],
    ) -> Any:
        """Normalize tool input to Thread's canonical format.

        Delegates to :func:`cursor_blocks.normalize_tool_input`.
        """
        return cursor_blocks.normalize_tool_input(
            original_name, canonical_name, input_data, msg
        )

    def _generate_id(self, conv: Dict[str, Any]) -> str:
        """Generate a conversation ID from available data."""
        import hashlib

        # Use workspace + title + timestamp if available
        parts = [
            conv.get("workspace_hash", ""),
            conv.get("title", ""),
            str(conv.get("created_at", "")),
        ]
        hash_input = "|".join(parts)
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]

    def _parse_markdown_export(self, markdown: str) -> List[NormalizedMessage]:
        """
        Parse Cursor's built-in Markdown export format.

        Cursor exports chats as Markdown with user/assistant turns.
        Format is typically:

        # Chat Title

        ## User
        Message content...

        ## Assistant
        Response content...
        ```python
        code blocks...
        ```
        """
        import re

        messages: List[NormalizedMessage] = []

        # Extract title from first heading
        title_match = re.match(r"^#\s+(.+?)$", markdown, re.MULTILINE)
        title = title_match.group(1) if title_match else "Exported Chat"

        # Generate conversation ID from content
        import hashlib
        conv_id = hashlib.sha256(markdown.encode()).hexdigest()[:16]

        # Split by ## headings for user/assistant turns
        # Pattern matches ## followed by role (User, Assistant, System, etc.)
        turn_pattern = r"##\s+(User|Assistant|System|Human|AI)\s*\n"
        parts = re.split(turn_pattern, markdown, flags=re.IGNORECASE)

        # First part is the title/preamble, skip it
        # Then alternating: role, content, role, content...
        if len(parts) > 1:
            turns = parts[1:]  # Skip preamble
            msg_idx = 0

            for i in range(0, len(turns) - 1, 2):
                role_raw = turns[i].strip().lower()
                content = turns[i + 1].strip() if i + 1 < len(turns) else ""

                if not content:
                    continue

                role = _extract_role({"role": role_raw})
                msg_id = f"{conv_id}_{msg_idx}"

                # Parse code blocks in markdown
                content_blocks = self._parse_markdown_content(content)

                content_text = self.extract_text_from_blocks(content_blocks)
                if not content_text:
                    content_text = content

                msg_record: NormalizedMessage = {
                    "source_provider": self.PROVIDER_NAME,
                    "provider_message_id": msg_id,
                    "provider_message_id_lower": msg_id.lower(),
                    "provider_conversation_id": conv_id,
                    "content_hash": self.hash_message(msg_id, role, content, None),
                    "role": role,
                    "content_text": content_text,
                    "content_blocks": content_blocks,
                    "created_at": None,
                    "updated_at": None,
                    "message_order": msg_idx,
                    "provider_data": {
                        "raw_markdown": content,
                        "conversation_title": title,
                    },
                    "provider_parent_id": None,
                    "provider_parent_id_lower": None,
                    "is_active_path": True,
                    "conversation_title": title,
                    "conversation_metadata": {"title": title, "format": "markdown"},
                }

                messages.append(msg_record)
                msg_idx += 1

        return messages

    def _parse_markdown_content(self, content: str) -> List[ContentBlock]:
        """Parse markdown content into content blocks, extracting code blocks."""
        import re

        blocks: List[ContentBlock] = []
        seq = 0

        # Pattern for fenced code blocks
        code_pattern = r"```(\w*)\n(.*?)```"

        last_end = 0
        for match in re.finditer(code_pattern, content, re.DOTALL):
            # Add text before code block
            text_before = content[last_end:match.start()].strip()
            if text_before:
                blocks.append(self.create_text_block(text_before, seq))
                seq += 1

            # Add code block
            language = match.group(1) or None
            code = match.group(2).strip()
            blocks.append({
                "type": "code",
                "code": code,
                "language": language,
                "seq": seq,
            })
            seq += 1

            last_end = match.end()

        # Add remaining text
        remaining = content[last_end:].strip()
        if remaining:
            blocks.append(self.create_text_block(remaining, seq))

        # If no blocks created, just use the whole content
        if not blocks:
            blocks.append(self.create_text_block(content, 0))

        return blocks
