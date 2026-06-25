"""
Provider configuration dataclasses.

ProviderConfig captures provider-specific characteristics that affect
parsing and validation. This moves configuration out of base.py and
makes it owned by each provider.
"""

from dataclasses import dataclass, field
from typing import Dict, Literal, Optional, Set

# Type alias for thinking block expectations
ThinkingExpectation = Literal["never", "required", "model_specific", "optional"]


@dataclass(frozen=True)
class ProviderConfig:
    """Provider-specific parsing and validation configuration.

    Each parser has a PROVIDER_CONFIG class attribute that configures
    validation behavior, expected roles/block types, and model-specific
    thinking requirements.

    Attributes:
        provider_name: Unique identifier for the provider
        thinking_expectation: How to validate thinking blocks
            - "never": Error if thinking blocks exist (wrong parser/source)
            - "required": Error if no thinking blocks (incomplete export)
            - "model_specific": Validate per-message based on model field
            - "optional": No validation
        thinking_exempt_models: Models that don't require thinking blocks
            (only used when thinking_expectation="model_specific")
        has_branching: Whether conversations can have branches/alternate paths
        has_parent_references: Whether messages have parent references
        expected_roles: Valid role values for this provider
        expected_block_types: Valid content block types for this provider
        timestamp_format: How timestamps are formatted in exports
            - "unix_seconds": Unix timestamp in seconds
            - "unix_millis": Unix timestamp in milliseconds
            - "iso": ISO 8601 string
            - "mixed": Can be any of the above (auto-detect)
    """

    provider_name: str
    thinking_expectation: ThinkingExpectation = "optional"
    thinking_exempt_models: Set[str] = field(default_factory=set)
    has_branching: bool = False
    has_parent_references: bool = False
    expected_roles: Set[str] = field(
        default_factory=lambda: {"user", "assistant", "system"}
    )
    expected_block_types: Set[str] = field(default_factory=set)
    timestamp_format: Literal["unix_seconds", "unix_millis", "iso", "mixed"] = "mixed"

    def requires_thinking(self, model: Optional[str]) -> bool:
        """Check if a specific model requires thinking blocks.

        Default behavior: unknown models REQUIRE thinking (safer - catches missing data).
        Only whitelisted non-reasoning models are exempt.

        Args:
            model: Model name/slug from the message

        Returns:
            True if thinking blocks are required for this model
        """
        if self.thinking_expectation != "model_specific":
            return self.thinking_expectation == "required"

        if not model:
            return True  # Unknown model = require thinking

        model_lower = model.lower()

        # Check if model is in the exempt list
        for exempt_model in self.thinking_exempt_models:
            exempt_lower = exempt_model.lower()
            if model_lower == exempt_lower or model_lower.startswith(f"{exempt_lower}-"):
                return False  # Known non-reasoning model

        return True  # Default: require thinking


# =============================================================================
# Provider-specific configurations
# =============================================================================

# Common non-reasoning models shared across providers
_COMMON_EXEMPT_MODELS = {
    # OpenAI non-reasoning models
    "gpt-4",
    "gpt-4-turbo",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-3.5-turbo",
    "gpt-3.5",
    "chatgpt-4o-latest",
    # Anthropic non-thinking models (note: Opus 4.5 DOES have thinking)
    "claude-3-5-sonnet",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku",
    "claude-haiku-3-5",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-haiku",
    "claude-2",
    "claude-2.1",
    "claude-instant",
    "claude-sonnet-4-5-20250929",
}


CHATGPT_CONFIG = ProviderConfig(
    provider_name="chatgpt",
    thinking_expectation="model_specific",
    thinking_exempt_models=_COMMON_EXEMPT_MODELS,
    has_branching=True,
    has_parent_references=True,
    expected_roles={"user", "assistant", "system", "tool"},
    expected_block_types={
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "image",
        "system_context",
    },
    timestamp_format="unix_seconds",
)


CLAUDE_CONFIG = ProviderConfig(
    provider_name="claude",
    # Claude.ai web exports NEVER include thinking blocks (server-side only)
    thinking_expectation="never",
    thinking_exempt_models=set(),  # Not applicable - expectation is "never"
    has_branching=False,
    has_parent_references=False,
    expected_roles={"user", "assistant", "human"},  # Claude uses "human" for user
    expected_block_types={
        "text",
        "tool_use",
        "tool_result",
        "system_metadata",
    },
    timestamp_format="iso",
)


CLAUDE_CODE_CONFIG = ProviderConfig(
    provider_name="claude-code",
    thinking_expectation="model_specific",
    thinking_exempt_models=_COMMON_EXEMPT_MODELS,
    has_branching=True,  # Tree via uuid/parentUuid
    has_parent_references=True,
    expected_roles={"user", "assistant", "system"},
    expected_block_types={
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "ide_context",
        "file_snapshot",
        "context_summary",
        "system_context",
        "parse_error",
        "image",  # pasted screenshots/images in a Claude Code session
        "queue_operation",  # Claude Code's message-queue feature (queued while the agent works)
        "progress",  # hook/tool progress telemetry (e.g. PostToolUse hook callbacks)
    },
    timestamp_format="iso",
)


CURSOR_CONFIG = ProviderConfig(
    provider_name="cursor",
    thinking_expectation="model_specific",
    thinking_exempt_models=_COMMON_EXEMPT_MODELS,
    has_branching=False,
    has_parent_references=False,
    expected_roles={"user", "assistant", "system", "tool"},
    expected_block_types={
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "code",
    },
    timestamp_format="mixed",  # Can be ISO string or Unix timestamp
)


# Registry of all provider configs
_PROVIDER_CONFIGS: Dict[str, ProviderConfig] = {
    "chatgpt": CHATGPT_CONFIG,
    "claude": CLAUDE_CONFIG,
    "claude-code": CLAUDE_CODE_CONFIG,
    "cursor": CURSOR_CONFIG,
}


def get_provider_config(provider: str) -> ProviderConfig:
    """Get the configuration for a provider.

    Args:
        provider: Provider name (chatgpt, claude, claude-code, cursor)

    Returns:
        ProviderConfig for the provider

    Raises:
        KeyError: If provider is not known
    """
    if provider not in _PROVIDER_CONFIGS:
        raise KeyError(
            f"Unknown provider: {provider}. "
            f"Known providers: {list(_PROVIDER_CONFIGS.keys())}"
        )
    return _PROVIDER_CONFIGS[provider]
