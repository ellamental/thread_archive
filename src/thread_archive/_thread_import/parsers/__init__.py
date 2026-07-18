"""
Provider-specific parsers for chat export files.

Each parser transforms a provider's export format into a standardized
message format with unified content_blocks schema.

## Architecture

The parser system has clear boundaries:

1. **Types** (`types/`): TypedDict schemas for provider input formats
2. **Config** (`config/`): Provider-specific configuration (ProviderConfig)
3. **Validators** (`validators/`): Pluggable validation rules

## Content Block Types

- text: Plain text content
- thinking: Model thinking/reasoning
- tool_use: Tool/function call
- tool_result: Tool/function result
- image: Image content
- file: File attachment
- code: Code execution
- system_context: System/user context
- ide_context: IDE state (opened files, selections)
- file_snapshot: File state snapshots
- context_summary: Conversation summaries
- parse_error: Preserved malformed data

## Field Mapping

- Each parser uses explicit FieldMapping declarations
- Maps provider JSON fields to our semantic model
- Documents what each field MEANS, not just where it comes from
"""

from typing import Type

# Base types and infrastructure
from .base import (
    CodeBlock,
    ContentBlock,
    FieldMapping,
    FileBlock,
    ImageBlock,
    NormalizedMessage,
    ProviderParser,
    SystemContextBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    ValidationSeverity,
)

# Provider parsers (ProviderParser subclasses, one per provider)
from .chatgpt import ChatGPTParser
from .claude import ClaudeParser
from .claude_code import ClaudeCodeParser

# Provider configurations
from .config import (
    CHATGPT_CONFIG,
    CLAUDE_CODE_CONFIG,
    CLAUDE_CONFIG,
    ProviderConfig,
    ThinkingExpectation,
    get_provider_config,
    register_provider_config,
    registered_providers,
)

# Validators
from .validators import (
    BaseValidator,
    ContentValidator,
    ReferentialIntegrityValidator,
    ThinkingBlockValidator,
    TypeValidator,
)

# Registry of parser classes (not instances - we create instances with config).
# Parsers that live in this island are seeded here; a provider defined outside it
# registers via :func:`register_parser` (a push, for the same isolation reason
# `register_provider_config` is one).
PARSER_CLASSES: dict[str, Type[ProviderParser]] = {
    "chatgpt": ChatGPTParser,
    "claude": ClaudeParser,
    "claude-code": ClaudeCodeParser,
}


def register_parser(provider: str, parser_class: Type[ProviderParser]) -> None:
    """Register a parser class under ``provider``, replacing any prior one.

    Idempotent, so a repeated registry load is a no-op rather than an error.
    """
    PARSER_CLASSES[provider] = parser_class


def registered_parsers() -> list[str]:
    """Every provider name with a registered parser, sorted."""
    return sorted(PARSER_CLASSES)


def get_parser(provider: str, strict: bool = False) -> ProviderParser:
    """
    Get a parser instance for a provider.

    Args:
        provider: Provider name (chatgpt, claude, claude-code, …)
        strict: If True, validation warnings become errors

    Returns:
        Parser instance configured with the given strictness level
    """
    if provider not in PARSER_CLASSES:
        raise ValueError(
            f"Unknown provider '{provider}'. Available: {registered_parsers()}"
        )
    return PARSER_CLASSES[provider](strict=strict)


__all__ = [
    # Base types
    "ProviderParser",
    "NormalizedMessage",
    "ContentBlock",
    # Field mapping infrastructure
    "FieldMapping",
    "ValidationSeverity",
    # Block types
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ImageBlock",
    "FileBlock",
    "CodeBlock",
    "SystemContextBlock",
    # Provider configurations
    "ProviderConfig",
    "ThinkingExpectation",
    "CHATGPT_CONFIG",
    "CLAUDE_CONFIG",
    "CLAUDE_CODE_CONFIG",
    "get_provider_config",
    "register_provider_config",
    "registered_providers",
    # Validators
    "BaseValidator",
    "ThinkingBlockValidator",
    "ReferentialIntegrityValidator",
    "TypeValidator",
    "ContentValidator",
    # Parsers (legacy interface)
    "ChatGPTParser",
    "ClaudeParser",
    "ClaudeCodeParser",
    "PARSER_CLASSES",
    "register_parser",
    "registered_parsers",
    "get_parser",
]

