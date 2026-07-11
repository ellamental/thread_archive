"""
ParserPipeline implementation.

Composes Parser -> Transformer[] -> Normalizer -> Validator[] into
a single processing pipeline.
"""

from typing import Any, Dict, Generic, List, Optional, TypeVar

from ..base import ImportResult, NormalizedMessage
from ..config import ProviderConfig
from .interfaces import (
    Normalizer,
    Parser,
    Transformer,
    ValidationContext,
    Validator,
)

T_Input = TypeVar("T_Input")


class ParserPipeline(Generic[T_Input]):
    """Composes parsing phases into a complete pipeline.

    The pipeline processes data through:
    1. Parse: Raw data -> RawMessage[]
    2. Transform: RawMessage[] -> RawMessage[] (optional, multiple)
    3. Normalize: RawMessage -> NormalizedMessage (for each message)
    4. Validate: NormalizedMessage[] -> validation results

    Example usage:
        pipeline = ParserPipeline(
            parser=ChatGPTRawParser(),
            transformers=[
                ActivePathTransformer(),
                ToolCoalescingTransformer(),
            ],
            normalizer=ChatGPTNormalizer(),
            validators=[
                ThinkingBlockValidator(CHATGPT_CONFIG),
                ReferentialIntegrityValidator(),
            ],
            config=CHATGPT_CONFIG,
        )
        result = pipeline.process(data)
    """

    def __init__(
        self,
        parser: Parser[T_Input],
        normalizer: Normalizer,
        config: ProviderConfig,
        transformers: Optional[List[Transformer]] = None,
        validators: Optional[List[Validator]] = None,
    ):
        """Initialize the pipeline.

        Args:
            parser: Parser for converting raw data to RawMessages
            normalizer: Normalizer for converting to NormalizedMessages
            config: Provider-specific configuration
            transformers: Optional list of transformers (applied in order)
            validators: Optional list of validators (all applied)
        """
        self.parser = parser
        self.transformers = transformers or []
        self.normalizer = normalizer
        self.validators = validators or []
        self.config = config

    def process(self, data: T_Input) -> ImportResult:
        """Process data through the complete pipeline.

        Args:
            data: Raw provider export data

        Returns:
            ImportResult with messages, errors, warnings, and coverage stats
        """
        # Phase 1: Parse
        raw_messages = self.parser.parse(data)

        # Phase 2: Transform
        for transformer in self.transformers:
            raw_messages = transformer.transform(raw_messages)

        # Phase 3: Normalize
        normalized_messages: List[NormalizedMessage] = []
        for raw in raw_messages:
            normalized = self.normalizer.normalize(raw)
            normalized_messages.append(normalized)

        # Phase 4: Validate
        all_errors, all_warnings = self._validate_all(normalized_messages)

        # Calculate field coverage
        field_coverage = self._calculate_field_coverage(normalized_messages)

        # Count unique conversations
        conversations = set(
            msg.get("provider_conversation_id", "unknown")
            for msg in normalized_messages
        )

        return ImportResult(
            messages=normalized_messages,
            validation_errors=all_errors,
            validation_warnings=all_warnings,
            field_coverage=field_coverage,
            conversations_processed=len(conversations),
            messages_processed=len(normalized_messages),
        )

    def _validate_all(
        self, messages: List[NormalizedMessage]
    ) -> tuple[List[str], List[str]]:
        """Run all validators on messages.

        Groups messages by conversation and validates each group.

        Returns:
            Tuple of (errors, warnings)
        """
        # Group by conversation
        conversations: Dict[str, List[NormalizedMessage]] = {}
        for msg in messages:
            conv_id = msg.get("provider_conversation_id", "unknown")
            if conv_id not in conversations:
                conversations[conv_id] = []
            conversations[conv_id].append(msg)

        all_errors: List[str] = []
        all_warnings: List[str] = []

        # Validate each conversation
        for conv_id, conv_messages in conversations.items():
            context = ValidationContext(
                conversation_id=conv_id,
                source_provider=self.config.provider_name,
            )

            # Sort by message_order for proper validation
            sorted_msgs = sorted(
                conv_messages, key=lambda m: m.get("message_order") or 0
            )

            # Run each validator
            for validator in self.validators:
                validator.validate(sorted_msgs, context)

            all_errors.extend(context.errors)
            all_warnings.extend(context.warnings)

        return all_errors, all_warnings

    def _calculate_field_coverage(
        self, messages: List[NormalizedMessage]
    ) -> Dict[str, float]:
        """Calculate percentage of messages with each field populated."""
        if not messages:
            return {}

        tracked_fields = [
            "created_at",
            "updated_at",
            "content_text",
            "content_blocks",
            "provider_parent_id",
            "message_order",
        ]

        coverage: Dict[str, int] = {f: 0 for f in tracked_fields}
        total = len(messages)

        for msg in messages:
            for field_name in tracked_fields:
                value = msg.get(field_name)  # type: ignore
                if value is not None and value != "" and value != []:
                    coverage[field_name] += 1

        return {f: count / total for f, count in coverage.items()}


class BaseNormalizer:
    """Base class for normalizers with common utilities.

    Provides helper methods for creating content blocks, hashing,
    and other common normalization tasks.
    """

    def __init__(self, config: ProviderConfig):
        """Initialize with provider config.

        Args:
            config: Provider-specific configuration
        """
        self.config = config

    def normalize_role(self, role: Optional[str]) -> str:
        """Normalize role to standard values."""
        if not role:
            return "unknown"
        role_lower = role.lower()
        if role_lower in ("user", "human"):
            return "user"
        elif role_lower in ("assistant", "ai", "bot"):
            return "assistant"
        elif role_lower in ("system", "tool"):
            return role_lower
        return role_lower

    def hash_message(
        self, msg_id: str, role: Optional[str], content: Any, create_time: Any
    ) -> str:
        """Create deterministic hash for deduplication."""
        import hashlib
        import json
        import math

        # Normalize create_time
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

    def extract_text_from_blocks(self, blocks: List[Any]) -> str:
        """Extract primary display text from content blocks."""
        text_parts = []
        for block in blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
            elif block_type == "system_context":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
        return "\n\n".join(text_parts).strip()
