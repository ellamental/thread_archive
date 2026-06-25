"""
Claude conversation export parser.

Parses the full Claude data export from Claude.ai (web interface).

## Format

Export source: Claude.ai → Settings → Account → Export Data
File structure: ZIP archive containing:

- ``conversations.json`` - Chat conversations with messages (primary)
- ``memories.json`` - Claude's memory about the user (optional)
- ``projects.json`` - Claude Projects and attached docs (optional)
- ``users.json`` - Account information (optional)

## Edge Cases

No Thinking Blocks (CRITICAL)
    Claude.ai exports do NOT include extended thinking blocks. Thinking is
    stored server-side and excluded from exports. For thinking blocks, use
    Claude Code exports (``~/.claude/``) or Claude API responses.

No Branching
    Messages are strictly linear - no alternate paths or regeneration history.

Sender Normalization
    Claude uses ``"human"`` where we use ``"user"``. See :func:`_extract_role`.

Legacy Text Field
    Older exports use ``text`` instead of ``content`` array. Both handled
    transparently.

## Validation

- **Thinking blocks**: Never present. 0% expected; >0% indicates wrong parser.
- **Parent references**: Not applicable (linear only).
- **Branching**: None.

## Field Mapping

- ``sender`` → ``role`` (``"human"`` → ``"user"``)
- ``created_at`` → ``created_at`` (ISO timestamp)
- ``content`` or ``text`` → ``content_blocks``
"""

from typing import Any, Dict, List, Optional, Union, cast

from thread_import.timestamps import parse_timestamp_iso

from .base import (
    ContentBlock,
    FieldMapping,
    NormalizedMessage,
    ProviderParser,
    SemanticCheck,
    ValidationSeverity,
)
from .config import CLAUDE_CONFIG, ProviderConfig
from .types.claude import ClaudeConversation, ClaudeExport
from .validators import (
    ContentValidator,
    ReferentialIntegrityValidator,
    ThinkingBlockValidator,
    TypeValidator,
)


def _parse_claude_timestamp(ts: Optional[str]) -> Optional[str]:
    """Parse Claude timestamp to normalized ISO format."""
    return parse_timestamp_iso(ts)


def _extract_created_at(msg: Dict[str, Any]) -> Optional[str]:
    """Extract and normalize created_at timestamp."""
    return _parse_claude_timestamp(msg.get("created_at"))


def _extract_updated_at(msg: Dict[str, Any]) -> Optional[str]:
    """Extract and normalize updated_at timestamp."""
    return _parse_claude_timestamp(msg.get("updated_at"))


def _extract_role(msg: Dict[str, Any]) -> str:
    """Extract and normalize role from sender field.

    Claude uses "human" where our schema uses "user".

    >>> _extract_role({"sender": "human"})
    'user'
    >>> _extract_role({"sender": "assistant"})
    'assistant'
    >>> _extract_role({"sender": "Human"})  # case-insensitive
    'user'
    >>> _extract_role({})
    'unknown'
    """
    sender = msg.get("sender", "")
    if sender:
        sender_lower = sender.lower()
        if sender_lower == "human":
            return "user"
        elif sender_lower == "assistant":
            return "assistant"
    return sender or "unknown"


def _extract_text(msg: Dict[str, Any]) -> str:
    """Extract primary text content."""
    return msg.get("text", "")


class ClaudeParser(ProviderParser):
    """Parser for Claude data export.

    Handles both single conversations.json files and full export bundles
    containing memories, projects, and user data.

    Outputs normalized messages with content_blocks in the unified schema.

    ## Architecture

    Uses the pipeline architecture with:
    - PROVIDER_CONFIG: Claude-specific configuration (thinking="never", no branching)
    - Validators: ThinkingBlockValidator, ContentValidator, etc.
    - Types: ClaudeExport, ClaudeConversation for typed input

    ## Explicit Field Mappings

    This parser uses explicit semantic mappings to ensure we correctly interpret
    Claude's JSON structure. Each mapping documents what the field MEANS in
    our model, not just where it comes from in the provider JSON.
    """

    PROVIDER_NAME = "claude"

    # Provider configuration (replaces centralized PROVIDER_EXPECTATIONS)
    PROVIDER_CONFIG: ProviderConfig = CLAUDE_CONFIG

    # Explicit field mappings with semantic documentation
    MESSAGE_FIELD_MAPPINGS = [
        FieldMapping(
            model_field="created_at",
            extractor=_extract_created_at,
            required=False,
            semantic_doc=(
                "When the message was originally SENT. Claude's created_at is already "
                "the user-facing timestamp, same semantic as our model."
            ),
        ),
        FieldMapping(
            model_field="updated_at",
            extractor=_extract_updated_at,
            required=False,
            semantic_doc=(
                "When the message was last EDITED. Same semantic as our model."
            ),
        ),
        FieldMapping(
            model_field="role",
            extractor=_extract_role,
            required=True,
            semantic_doc=(
                "The actor role. Claude uses 'human'/'assistant', we normalize to "
                "'user'/'assistant' for consistency across providers."
            ),
        ),
        FieldMapping(
            model_field="text",
            extractor=_extract_text,
            required=False,
            semantic_doc=(
                "Primary text content. In Claude, this is a top-level field that "
                "may exist alongside or instead of content blocks."
            ),
        ),
    ]

    # Semantic checks for Claude-specific requirements
    SEMANTIC_CHECKS = [
        SemanticCheck(
            name="user_context_on_first_message",
            description=(
                "First user message should have user context. In Claude, this often "
                "comes from project memory or the memories.json file in the export."
            ),
            applies_to="first_user_message",
            severity=ValidationSeverity.error,
        ),
        SemanticCheck(
            name="thinking_on_assistant_messages",
            description=(
                "Claude 3.5+ Sonnet messages should have thinking blocks when using "
                "extended thinking. Not all conversations use this feature."
            ),
            applies_to="assistant_messages",
            severity=ValidationSeverity.warning,
        ),
    ]

    def __init__(self, strict: bool = False):
        """Initialize the Claude parser."""
        super().__init__(strict=strict)
        # Initialize validators from the new architecture
        self._validators = [
            ThinkingBlockValidator(self.PROVIDER_CONFIG, strict=strict),
            ReferentialIntegrityValidator(self.PROVIDER_CONFIG, strict=strict),
            TypeValidator(self.PROVIDER_CONFIG, strict=strict),
            ContentValidator(self.PROVIDER_CONFIG, strict=strict),
        ]

    def parse_export(
        self, data: Union[ClaudeExport, List[ClaudeConversation], Any]
    ) -> List[NormalizedMessage]:
        """
        Parse Claude export data into normalized message format.

        Uses explicit field mappings to ensure semantic correctness.

        Args:
            data: Either:
                - A list of conversations (from conversations.json)
                - A dict with 'conversations', 'memories', 'projects', 'users' keys
                  (full export bundle)

        Returns:
            List of NormalizedMessage dictionaries with content_blocks
        """
        # Handle both formats: raw conversations list or bundled export
        if isinstance(data, dict):
            conversations = data.get("conversations", [])
            memories = data.get("memories", {})
            projects = data.get("projects", [])
            users = data.get("users", [])
        else:
            # Assume it's a list of conversations
            conversations = data if isinstance(data, list) else []
            memories = {}
            projects = []
            users = []

        # Build lookup tables for enrichment
        project_map = {p["uuid"]: p for p in cast(List[Dict[str, Any]], projects or [])}
        user_map = {u["uuid"]: u for u in cast(List[Dict[str, Any]], users or [])}
        project_memories = memories.get("project_memories", {}) if isinstance(memories, dict) else {}

        messages: List[NormalizedMessage] = []

        for conv in conversations:
            conv_id = conv.get("uuid")
            conv_name = conv.get("name", "Untitled")
            conv_summary = conv.get("summary", "")

            # Get account info
            account_uuid = conv.get("account", {}).get("uuid")
            account_info = user_map.get(account_uuid, {}) if account_uuid else {}

            # Check if conversation is part of a project
            _project = conv.get("project")
            project_uuid = _project.get("uuid") if isinstance(_project, dict) else None
            project_info = project_map.get(project_uuid, {}) if project_uuid else {}
            project_memory = project_memories.get(project_uuid, "") if project_uuid else ""

            # Conversation-level metadata
            conv_metadata = {
                "title": conv_name,
                "summary": conv_summary,
                "project_uuid": project_uuid,
                "project_name": project_info.get("name"),
                "project_description": project_info.get("description"),
                "project_memory": project_memory,
                "account_uuid": account_uuid,
                "account_name": account_info.get("full_name"),
            }

            chat_messages = conv.get("chat_messages", [])

            for msg_idx, msg in enumerate(chat_messages):
                msg_id = msg.get("uuid")

                # Apply explicit field mappings for semantic correctness
                mapped_fields = self.apply_field_mappings(cast(Dict[str, Any], msg), self.MESSAGE_FIELD_MAPPINGS)
                role = mapped_fields.get("role", "unknown")
                text = mapped_fields.get("text", "")
                created_at_iso = mapped_fields.get("created_at")
                updated_at_iso = mapped_fields.get("updated_at")

                raw_content_blocks = msg.get("content", [])
                # Note: attachments and files available in msg if needed

                # Convert Claude content blocks to normalized format
                content_blocks = self._normalize_content_blocks(
                    cast(List[Dict[str, Any]], raw_content_blocks), text)

                # Extract primary text from blocks
                content_text = self.extract_text_from_blocks(content_blocks)
                # If no text from blocks, use the top-level text field
                if not content_text and text:
                    content_text = text

                # Message order (use index since Claude doesn't provide explicit ordering)
                # Semantic: Claude messages are ordered by position in array, not by timestamp
                message_order = msg_idx

                # Build the normalized message
                msg_record: NormalizedMessage = cast(NormalizedMessage, {
                    "source_provider": self.PROVIDER_NAME,
                    "provider_message_id": msg_id,
                    "provider_message_id_lower": msg_id.lower() if msg_id else None,
                    "provider_conversation_id": conv_id,
                    "content_hash": self.hash_message(cast(str, msg_id), role, text, msg.get("created_at")),
                    "role": role,
                    "content_text": content_text,
                    "content_blocks": content_blocks,
                    "created_at": created_at_iso,
                    "updated_at": updated_at_iso,
                    "message_order": message_order,
                    # Provider data preservation - exact original message plus conversation context
                    "provider_data": {
                        "message": msg,
                        "conversation_title": conv_name,
                    },
                    # Claude doesn't have branching like ChatGPT
                    "provider_parent_id": None,
                    "provider_parent_id_lower": None,
                    "is_active_path": True,
                    # Conversation metadata
                    "conversation_title": conv_name,
                    "conversation_metadata": conv_metadata,
                })

                messages.append(msg_record)

        return messages

    def _normalize_content_blocks(
        self,
        raw_blocks: List[Dict],
        fallback_text: str
    ) -> List[ContentBlock]:
        """
        Convert Claude content blocks to normalized ContentBlock format.

        Claude block types:
        - text: Plain text content
        - thinking: Extended thinking content (Claude 3.5+)
        - tool_use: Tool/function call
        - tool_result: Tool/function result
        - token_budget: Internal token tracking (stored as system_metadata)
        """
        blocks: List[ContentBlock] = []
        seq = 0

        # If there are no blocks but there's text, create a text block
        if not raw_blocks and fallback_text:
            blocks.append(self.create_text_block(fallback_text, seq))
            return blocks

        handlers = {
            "text": self._block_text,
            "tool_use": self._block_tool_use,
            "tool_result": self._block_tool_result,
            "token_budget": self._block_token_budget,
            "thinking": self._block_thinking,
        }
        for raw_block in raw_blocks:
            block_type = raw_block.get("type", "")
            handler = handlers.get(block_type, self._block_unknown)
            block = handler(raw_block, seq)
            if block is not None:
                blocks.append(block)
                seq += 1

        return blocks

    def _block_text(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        text = raw_block.get("text", "")
        if not text:
            return None
        return self.create_text_block(
            text,
            seq,
            start_timestamp=raw_block.get("start_timestamp"),
            stop_timestamp=raw_block.get("stop_timestamp"),
        )

    def _block_tool_use(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        name = raw_block.get("name", "unknown")
        input_data = raw_block.get("input", {})
        return self.create_tool_use_block(
            name,
            input_data,
            seq,
            message=raw_block.get("message"),
            display_content=raw_block.get("display_content"),
            start_timestamp=raw_block.get("start_timestamp"),
            stop_timestamp=raw_block.get("stop_timestamp"),
        )

    def _block_tool_result(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        name = raw_block.get("name", "unknown")
        content = raw_block.get("content")
        is_error = raw_block.get("is_error", False)
        return self.create_tool_result_block(
            name,
            content,
            seq,
            is_error=is_error,
            display_content=raw_block.get("display_content"),
        )

    def _block_token_budget(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        # Store token budget as system_metadata instead of dropping
        block: ContentBlock = {
            "type": "system_metadata",
            "metadata_type": "token_budget",
            "data": raw_block,
            "seq": seq,
        }
        return block

    def _block_thinking(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        thinking_text = raw_block.get("thinking", "")
        if not thinking_text:
            return None
        block: ContentBlock = {
            "type": "thinking",
            "text": thinking_text,
            "seq": seq,
        }
        # Preserve signature if present
        if raw_block.get("signature"):
            cast(Dict[str, Any], block)["signature"] = raw_block["signature"]
        return block

    def _block_unknown(self, raw_block: Dict, seq: int) -> Optional[ContentBlock]:
        # Unknown block type - preserve as text if it has content
        text = raw_block.get("text") or raw_block.get("content")
        if text and isinstance(text, str):
            return self.create_text_block(text, seq)
        return None

    # parse_export_with_validation is inherited from ProviderParser (shared logic
    # driving self._validators); no provider-specific override needed.
