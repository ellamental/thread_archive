"""
ChatGPT conversation export parser.

Parses the conversations.json export file from ChatGPT.

## Format

Export source: ChatGPT web UI → Settings → Data controls → Export data
File structure: ZIP archive containing ``conversations.json``

The ``conversations.json`` contains an array of conversation objects, each with:
- ``mapping``: Dict of message_id → message node (tree structure)
- ``current_node``: ID of the leaf node in the "active" conversation path
- ``title``, ``create_time``, ``update_time``: Conversation metadata

## Edge Cases

Tree Structure via ``mapping``
    ChatGPT stores messages as a tree, not a linear list. Each message has a
    ``parent`` field pointing to its predecessor. Branching occurs when users
    edit messages or regenerate responses, creating alternate paths.

Coalescing Tool Messages
    ChatGPT exports tool calls, thinking, and results as separate messages
    linked via parent_id. This parser coalesces them into ``content_blocks``
    on the parent assistant message. Original message boundaries are preserved
    via ``provider_message_id`` on each block.

Active Path Detection
    ``current_node`` identifies the conversation's "main" thread. Messages not
    on this path are marked ``is_active_path=False``. The path is computed by
    walking from ``current_node`` back to root.

Thinking Content Types
    o1/o1-pro models emit thinking as separate messages with content_type in:
    ``thoughts``, ``analysis``, ``reasoning_recap``. These are coalesced as
    ``thinking`` blocks on the parent assistant message.

System Context Messages
    Custom instructions appear as messages with content_type in:
    ``user_editable_context``, ``model_editable_context``. Converted to
    ``system_context`` blocks.

## Validation Notes

Thinking blocks
    Model-specific. Required for o1/o1-pro models, not expected for GPT-4/GPT-3.5.
    See ``ProviderConfig`` for exempt models.

Parent references
    Always valid within a conversation. Missing parents indicate data corruption.

Branching
    Full tree structure preserved. Use ``is_active_path`` to filter to main thread.

## Field Mapping

- ``create_time`` → ``created_at`` (Unix timestamp, immutable)
- ``update_time`` → ``updated_at`` (mutable, changes on edits)
- ``mapping[id].parent`` → ``provider_parent_id`` (tree structure)
- ``current_node`` → Used to compute ``is_active_path``
"""

from typing import Any, Dict, List, Optional, Set, Union, cast

from thread_import.timestamps import parse_timestamp_iso

from .base import (
    ContentBlock,
    FieldMapping,
    NormalizedMessage,
    ProviderParser,
    SemanticCheck,
    ValidationSeverity,
)
from .chatgpt_content import (
    append_image_parts,
    append_metadata_tool_calls,
    append_text_content,
    build_content_blocks,
    collect_coalesce_candidates,
    extract_content_blocks,
    extract_special_block,
    extract_text_from_content,
    is_primary_message,
)
from .config import CHATGPT_CONFIG, ProviderConfig
from .types.chatgpt import ChatGPTConversation, ChatGPTExport
from .validators import (
    ContentValidator,
    ReferentialIntegrityValidator,
    ThinkingBlockValidator,
    TypeValidator,
)

# Content types that indicate thinking/reasoning messages
THINKING_CONTENT_TYPES = {"thoughts", "analysis", "reasoning_recap"}

# Content types that indicate structural/scaffolding messages (not displayed)
STRUCTURAL_CONTENT_TYPES = {"code", "commentary", "metadata", "system"}

# Content types that represent system context
CONTEXT_CONTENT_TYPES = {"user_editable_context", "model_editable_context"}


def _extract_create_time_iso(raw_msg: Dict[str, Any]) -> Optional[str]:
    """Extract create_time and convert to ISO format."""
    create_time = raw_msg.get("create_time")
    if create_time is None:
        return None
    # Epoch is an absolute instant — parse to aware UTC, not naive-local (which
    # would silently skew every ChatGPT timestamp by the host's UTC offset).
    return parse_timestamp_iso(create_time)


def _extract_update_time_iso(raw_msg: Dict[str, Any]) -> Optional[str]:
    """Extract update_time and convert to ISO format."""
    update_time = raw_msg.get("update_time")
    if update_time is None:
        return None
    # Aware UTC (see _extract_create_time_iso) — never naive-local.
    return parse_timestamp_iso(update_time)


def _extract_message_order(raw_msg: Dict[str, Any]) -> Optional[int]:
    """Convert create_time to microsecond-precision message order."""
    create_time = raw_msg.get("create_time")
    if create_time is None or not isinstance(create_time, (int, float)):
        return None
    try:
        return int(create_time * 1_000_000)
    except (ValueError, OverflowError):
        return None


def _extract_role(raw_msg: Dict[str, Any]) -> Optional[str]:
    """Extract role from nested author structure."""
    return raw_msg.get("author", {}).get("role")


def _extract_author_name(raw_msg: Dict[str, Any]) -> Optional[str]:
    """Extract author name (for tool attribution)."""
    return raw_msg.get("author", {}).get("name")


def _extract_model(raw_msg: Dict[str, Any]) -> Optional[str]:
    """Extract model name from message metadata.model_slug."""
    return (raw_msg.get("metadata") or {}).get("model_slug")


class ChatGPTParser(ProviderParser):
    """Parser for ChatGPT conversations.json export.

    Implements coalescing: tool calls, thinking, and results from child messages
    are merged into content_blocks on their parent message.

    ## Architecture

    Uses the pipeline architecture with:
    - PROVIDER_CONFIG: ChatGPT-specific configuration (thinking expectations, etc.)
    - Validators: ThinkingBlockValidator, ReferentialIntegrityValidator, etc.
    - Types: ChatGPTExport, ChatGPTConversation for typed input

    ## Explicit Field Mappings

    This parser uses explicit semantic mappings to ensure we correctly interpret
    ChatGPT's JSON structure. Each mapping documents what the field MEANS in
    our model, not just where it comes from in the provider JSON.
    """

    PROVIDER_NAME = "chatgpt"

    # Provider configuration (replaces centralized PROVIDER_EXPECTATIONS)
    PROVIDER_CONFIG: ProviderConfig = CHATGPT_CONFIG

    # Explicit field mappings with semantic documentation
    # These are applied to the raw message dict (from mapping[id].message)
    MESSAGE_FIELD_MAPPINGS = [
        FieldMapping(
            model_field="created_at",
            extractor=_extract_create_time_iso,
            required=False,
            semantic_doc=(
                "When the message was originally SENT by the user/assistant. "
                "This is create_time in ChatGPT, which is immutable and represents "
                "the user-facing timestamp. NOT the same as when the DB record was created."
            ),
        ),
        FieldMapping(
            model_field="updated_at",
            extractor=_extract_update_time_iso,
            required=False,
            semantic_doc=(
                "When the message was last EDITED. This is update_time in ChatGPT. "
                "May be null if message was never edited. Different from DB update timestamps."
            ),
        ),
        FieldMapping(
            model_field="message_order",
            extractor=_extract_message_order,
            required=False,
            semantic_doc=(
                "Microsecond-precision ordering value derived from create_time. "
                "Used for consistent message ordering within a conversation."
            ),
        ),
        FieldMapping(
            model_field="role",
            extractor=_extract_role,
            required=False,
            semantic_doc=(
                "The actor role: 'user', 'assistant', 'system', or 'tool'. "
                "Extracted from message.author.role in ChatGPT's structure."
            ),
        ),
        FieldMapping(
            model_field="author_name",
            extractor=_extract_author_name,
            required=False,
            semantic_doc=(
                "The tool/plugin name when role='tool'. "
                "Used to identify which tool produced a tool_result block."
            ),
        ),
        FieldMapping(
            model_field="model",
            extractor=_extract_model,
            required=False,
            semantic_doc=(
                "The model that generated this message (e.g. 'gpt-4', 'gpt-4o'). "
                "Extracted from message.metadata.model_slug in ChatGPT's structure. "
                "Only present on assistant messages."
            ),
        ),
    ]

    # Semantic checks for ChatGPT-specific requirements
    SEMANTIC_CHECKS = [
        SemanticCheck(
            name="user_context_on_first_message",
            description=(
                "First user message should have user context (custom instructions, "
                "memory, etc.) from user_editable_context or model_editable_context blocks"
            ),
            applies_to="first_user_message",
            severity=ValidationSeverity.error,
        ),
        SemanticCheck(
            name="thinking_on_assistant_messages",
            description=(
                "Assistant messages from o1/o3 models should have thinking blocks. "
                "Not required for older models, so this is a warning not an error."
            ),
            applies_to="assistant_messages",
            severity=ValidationSeverity.warning,
        ),
    ]

    def __init__(self, strict: bool = False):
        """Initialize the ChatGPT parser."""
        super().__init__(strict=strict)
        # Initialize validators from the new architecture
        self._validators = [
            ThinkingBlockValidator(self.PROVIDER_CONFIG, strict=strict),
            ReferentialIntegrityValidator(self.PROVIDER_CONFIG, strict=strict),
            TypeValidator(self.PROVIDER_CONFIG, strict=strict),
            ContentValidator(self.PROVIDER_CONFIG, strict=strict),
        ]

    def parse_export(
        self, data: Union[ChatGPTExport, List[ChatGPTConversation], Any]
    ) -> List[NormalizedMessage]:
        """
        Parse ChatGPT export data into normalized message format.

        The coalescing strategy:
        1. First pass: Parse all messages, build parent->children map
        2. Second pass: For each primary message (user/assistant with text content),
           collect child tool/thinking messages and convert to content_blocks
        3. Only emit separate rows for true branch points (multiple children with different paths)
        """
        # Handle both formats: raw list or bundled {"conversations": [...]}
        if isinstance(data, dict) and "conversations" in data:
            conversations = data["conversations"]
        elif isinstance(data, list):
            conversations = data
        else:
            conversations = [data]

        all_messages: List[NormalizedMessage] = []

        for conv in conversations:
            conv_messages = self._parse_conversation(cast(Dict[str, Any], conv))
            all_messages.extend(conv_messages)

        return all_messages

    def _parse_conversation(self, conv: Dict) -> List[NormalizedMessage]:
        """Parse a single ChatGPT conversation with coalescing."""
        conv_id = cast(str, conv.get("id"))
        conv_title = conv.get("title", "Untitled")
        mapping = conv.get("mapping", {}) or {}

        # Conversation-level metadata
        conv_metadata = {
            "title": conv_title,
            "current_node": conv.get("current_node"),
            "moderation_results": conv.get("moderation_results"),
            "plugin_ids": conv.get("plugin_ids"),
            "gizmo_id": conv.get("gizmo_id"),
            "conversation_template_id": conv.get("conversation_template_id"),
            "safe_urls": conv.get("safe_urls"),
            "is_archived": conv.get("is_archived"),
        }

        # Build active path set
        active_path_ids = self._build_active_path(conv.get("current_node"), mapping)

        # First pass: Parse all raw messages
        raw_messages = self._parse_all_messages(mapping, conv_id, conv_title, conv_metadata, active_path_ids)

        # Build lookup structures
        msg_by_id: Dict[str, Dict] = {m["provider_message_id"]: m for m in raw_messages}
        children_by_parent: Dict[str, List[str]] = {}
        for msg in raw_messages:
            parent_id = msg.get("provider_parent_id")
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(msg["provider_message_id"])

        # Second pass: Coalesce and build final messages
        messages = self._coalesce_messages(
            raw_messages,
            msg_by_id,
            children_by_parent,
            active_path_ids,
            conv_id,
            conv_title,
            conv_metadata,
        )

        return messages

    def _build_active_path(self, current_node: Optional[str], mapping: Dict) -> Set[str]:
        """Walk from current_node to root to build active path set."""
        active_path_ids: Set[str] = set()
        node_id = current_node
        hop_guard = 0
        while node_id and node_id in mapping and hop_guard < 10_000:
            active_path_ids.add(node_id)
            node_id = mapping.get(node_id, {}).get("parent")
            hop_guard += 1
        return active_path_ids

    def _parse_all_messages(
        self,
        mapping: Dict,
        conv_id: str,
        conv_title: str,
        conv_metadata: Dict,
        active_path_ids: Set[str],
    ) -> List[Dict]:
        """
        First pass: Parse all messages from mapping into raw dict format.
        This includes stubs, tool messages, thinking messages, etc.

        Uses explicit field mappings where possible to ensure semantic correctness.
        """
        messages = []
        mapping_ids = set(mapping.keys())
        referenced_parents: Set[str] = set()
        parent_min_create: Dict[str, float] = {}

        for msg_id, msg_data in mapping.items():
            message = msg_data.get("message") or {}
            is_stub = not bool(message)

            author = message.get("author", {}) if not is_stub else {}
            content = message.get("content", {}) if not is_stub else {}

            # Normalize content type
            content_type = None
            if isinstance(content, dict):
                content_type = content.get("content_type")
            if not content_type:
                meta_ct = message.get("metadata", {}).get("content_type")
                if isinstance(meta_ct, str):
                    content_type = meta_ct

            # Get timestamps using explicit mappings
            # Note: We extract raw values here, mappings convert them
            create_time = message.get("create_time") if not is_stub else None
            update_time = message.get("update_time") if not is_stub else None

            # Provider parent ID - explicit mapping from msg_data.parent
            # Semantic: This is the conversation tree parent, NOT a "reply to" relationship
            provider_parent_id = msg_data.get("parent") or message.get("metadata", {}).get("parent_id")

            if provider_parent_id:
                referenced_parents.add(provider_parent_id)
            if provider_parent_id and isinstance(create_time, (int, float)):
                prev = parent_min_create.get(provider_parent_id)
                if prev is None or create_time < prev:
                    parent_min_create[provider_parent_id] = create_time

            # Apply field mappings for timestamp conversion
            # These mappings ensure correct semantic interpretation
            mapped_fields = self.apply_field_mappings(message, self.MESSAGE_FIELD_MAPPINGS)

            is_active_path = msg_id in active_path_ids
            msg_metadata = message.get("metadata", {}) or {}

            raw_msg = {
                "provider_message_id": msg_id,
                "provider_parent_id": provider_parent_id,
                # Use mapped values (with semantic correctness)
                "role": mapped_fields.get("role"),
                "author_name": mapped_fields.get("author_name"),
                "created_at": mapped_fields.get("created_at"),
                "updated_at": mapped_fields.get("updated_at"),
                "message_order": mapped_fields.get("message_order"),
                "model": mapped_fields.get("model"),
                "recipient": message.get("recipient"),
                # Raw values for internal processing
                "content": content,
                "content_type": content_type,
                "create_time": create_time,  # Keep raw for hash computation
                "update_time": update_time,  # Keep raw for reference
                "is_active_path": is_active_path,
                "is_stub": is_stub,
                "is_visually_hidden": msg_metadata.get("is_visually_hidden_from_conversation"),
                "is_user_system_message": msg_metadata.get("is_user_system_message"),
                "children": msg_data.get("children", []),
                "msg_metadata": msg_metadata,
                "author": author,
                "raw_message": message,
                "raw_msg_data": msg_data,
            }
            messages.append(raw_msg)

        # Add stub nodes for missing parents
        # These are synthetic records for orphaned parent references
        missing_parents = referenced_parents - mapping_ids
        for missing_id in missing_parents:
            if not missing_id:
                continue
            stub_ct = parent_min_create.get(missing_id)
            # Create a minimal message dict for field mapping
            stub_message = {"create_time": stub_ct, "update_time": stub_ct}
            stub_mapped = self.apply_field_mappings(stub_message, self.MESSAGE_FIELD_MAPPINGS)

            messages.append({
                "provider_message_id": missing_id,
                "provider_parent_id": None,
                "role": None,
                "author_name": None,
                "content": {},
                "content_type": None,
                "create_time": stub_ct,
                "update_time": stub_ct,
                "created_at": stub_mapped.get("created_at"),
                "updated_at": stub_mapped.get("updated_at"),
                "message_order": stub_mapped.get("message_order"),
                "is_active_path": False,
                "is_stub": True,
                "is_visually_hidden": None,
                "is_user_system_message": None,
                "children": [],
                "msg_metadata": {},
                "author": {},
                "raw_message": {},
                "raw_msg_data": {},
                "synthetic_stub": True,
            })

        return messages

    def _coalesce_messages(
        self,
        raw_messages: List[Dict],
        msg_by_id: Dict[str, Dict],
        children_by_parent: Dict[str, List[str]],
        active_path_ids: Set[str],
        conv_id: str,
        conv_title: str,
        conv_metadata: Dict,
    ) -> List[NormalizedMessage]:
        """
        Second pass: Build normalized messages with coalesced content_blocks.

        Strategy:
        - Primary messages (user/assistant with displayable text) become rows
        - Tool/thinking messages on the active path get coalesced into their parent
        - Non-active-path branches remain as separate rows for history
        """
        messages: List[NormalizedMessage] = []
        coalesced_ids: Set[str] = set()  # Track which messages were coalesced

        # Sort by message_order for consistent processing
        sorted_raw = sorted(raw_messages, key=lambda m: m.get("message_order") or 0)

        for raw_msg in sorted_raw:
            msg_id = raw_msg["provider_message_id"]

            # Skip if already coalesced into another message
            if msg_id in coalesced_ids:
                continue

            # Determine if this is a primary message (should become a row)
            is_primary = self._is_primary_message(raw_msg)

            if is_primary:
                # Collect children to coalesce (tool calls, thinking, results)
                children_to_coalesce = self._collect_coalesce_candidates(
                    msg_id,
                    children_by_parent,
                    msg_by_id,
                    active_path_ids
                )

                # Mark coalesced messages
                for child_id in children_to_coalesce:
                    coalesced_ids.add(child_id)

                # Build content blocks from primary + coalesced children
                content_blocks = self._build_content_blocks(
                    raw_msg,
                    [msg_by_id[cid] for cid in children_to_coalesce if cid in msg_by_id],
                )

                # Create normalized message
                normalized = self._create_normalized_message(
                    raw_msg,
                    content_blocks,
                    conv_id,
                    conv_title,
                    conv_metadata,
                )
                messages.append(normalized)
            else:
                # Non-primary message that wasn't coalesced - emit as separate row
                # This includes stubs, non-active-path branches, etc.
                content_blocks = self._build_content_blocks(raw_msg, [])
                normalized = self._create_normalized_message(
                    raw_msg,
                    content_blocks,
                    conv_id,
                    conv_title,
                    conv_metadata,
                )
                messages.append(normalized)

        return messages

    def _is_primary_message(self, raw_msg: Dict) -> bool:
        """Determine if a message should be a primary row (vs coalesced).

        Delegates to ``chatgpt_content.is_primary_message`` (stateless body).
        """
        return is_primary_message(raw_msg)

    def _collect_coalesce_candidates(
        self,
        parent_id: str,
        children_by_parent: Dict[str, List[str]],
        msg_by_id: Dict[str, Dict],
        active_path_ids: Set[str],
    ) -> List[str]:
        """Collect child messages to coalesce into parent.

        Delegates to ``chatgpt_content.collect_coalesce_candidates``.
        """
        return collect_coalesce_candidates(
            parent_id, children_by_parent, msg_by_id, active_path_ids
        )

    def _build_content_blocks(
        self,
        primary_msg: Dict,
        coalesced_msgs: List[Dict]
    ) -> List[ContentBlock]:
        """Build content_blocks from primary message and coalesced children.

        Delegates to ``chatgpt_content.build_content_blocks``.
        """
        return build_content_blocks(primary_msg, coalesced_msgs)

    def _extract_content_blocks(
        self,
        raw_msg: Dict,
        start_seq: int
    ) -> tuple[List[ContentBlock], int]:
        """Extract content blocks from a single raw message; returns (blocks, next_seq).

        Delegates to ``chatgpt_content.extract_content_blocks``.
        """
        return extract_content_blocks(raw_msg, start_seq)

    def _extract_special_block(
        self,
        raw_msg: Dict,
        content: Any,
        content_type: str,
        role: str,
        msg_id: Optional[str],
        seq: int,
    ) -> Optional[tuple[List[ContentBlock], int]]:
        """Handle the exclusive content cases that fully consume a message.

        Delegates to ``chatgpt_content.extract_special_block``.
        """
        return extract_special_block(
            raw_msg, content, content_type, role, msg_id, seq
        )

    def _append_metadata_tool_calls(
        self,
        blocks: List[ContentBlock],
        msg_metadata: Dict,
        msg_id: Optional[str],
        seq: int,
    ) -> int:
        """Append tool_use blocks from metadata tool_calls (alternate export formats).

        Delegates to ``chatgpt_content.append_metadata_tool_calls``.
        """
        return append_metadata_tool_calls(blocks, msg_metadata, msg_id, seq)

    def _append_text_content(
        self, blocks: List[ContentBlock], content: Any, seq: int
    ) -> int:
        """Append a text block from regular content, if any.

        Delegates to ``chatgpt_content.append_text_content``.
        """
        return append_text_content(blocks, content, seq)

    def _append_image_parts(
        self, blocks: List[ContentBlock], content: Any, seq: int
    ) -> int:
        """Append image blocks from multimodal content parts.

        Delegates to ``chatgpt_content.append_image_parts``.
        """
        return append_image_parts(blocks, content, seq)

    def _extract_text_from_content(self, content: Any) -> str:
        """Extract text content from ChatGPT content object.

        Delegates to ``chatgpt_content.extract_text_from_content``.
        """
        return extract_text_from_content(content)

    def _create_normalized_message(
        self,
        raw_msg: Dict,
        content_blocks: List[ContentBlock],
        conv_id: str,
        conv_title: str,
        conv_metadata: Dict,
    ) -> NormalizedMessage:
        """Create a NormalizedMessage from raw message data."""
        msg_id = raw_msg["provider_message_id"]

        # Extract primary text from blocks
        content_text = self.extract_text_from_blocks(content_blocks)

        # Normalize role
        role = self.normalize_role(raw_msg.get("role"))

        # Build provider_data (exact original structure plus conversation context)
        provider_data = {
            "message": raw_msg.get("raw_message", {}),
            "msg_data": raw_msg.get("raw_msg_data", {}),
            "conversation_title": conv_title,
        }
        if raw_msg.get("model"):
            provider_data["model"] = raw_msg["model"]
        if raw_msg.get("synthetic_stub"):
            provider_data["synthetic_stub"] = True

        return {
            "source_provider": "chatgpt",
            "provider_message_id": msg_id,
            "provider_message_id_lower": msg_id.lower() if msg_id else None,
            "provider_conversation_id": conv_id,
            "content_hash": self.hash_message(
                msg_id,
                cast(str, raw_msg.get("role")),
                raw_msg.get("content"),
                raw_msg.get("create_time"),
            ),
            "role": role,
            "content_text": content_text,
            "content_blocks": content_blocks,
            "created_at": raw_msg.get("created_at"),
            "updated_at": raw_msg.get("updated_at"),
            "message_order": raw_msg.get("message_order"),
            "provider_data": provider_data,
            "provider_parent_id": raw_msg.get("provider_parent_id"),
            "provider_parent_id_lower": raw_msg["provider_parent_id"].lower() if raw_msg.get("provider_parent_id") else None,
            "is_active_path": raw_msg.get("is_active_path"),
            "conversation_title": conv_title,
            "conversation_metadata": conv_metadata,
        }

    def _timestamp_to_iso(self, ts: Optional[float]) -> Optional[str]:
        """Convert Unix timestamp to ISO format (aware UTC, never naive-local)."""
        if ts is None:
            return None
        return parse_timestamp_iso(ts)

    def _timestamp_to_order(self, ts: Optional[float]) -> Optional[int]:
        """Convert Unix timestamp to message order (microseconds)."""
        if ts is None or not isinstance(ts, (int, float)):
            return None
        try:
            return int(ts * 1_000_000)
        except (ValueError, OverflowError):
            return None

    # parse_export_with_validation is inherited from ProviderParser (shared logic
    # driving self._validators); no provider-specific override needed.
