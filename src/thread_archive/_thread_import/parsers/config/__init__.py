"""
Provider configuration definitions.

Each provider has specific characteristics that affect parsing and validation.
ProviderConfig captures these in a type-safe, declarative way.
"""

from .base import (  # noqa: F401 — re-export surface
    CHATGPT_CONFIG,
    CLAUDE_CODE_CONFIG,
    CLAUDE_CONFIG,
    ProviderConfig,
    ThinkingExpectation,
    get_provider_config,
    register_provider_config,
    registered_providers,
)

__all__ = [
    "ProviderConfig",
    "ThinkingExpectation",
    "CHATGPT_CONFIG",
    "CLAUDE_CONFIG",
    "CLAUDE_CODE_CONFIG",
    "get_provider_config",
    "register_provider_config",
    "registered_providers",
]
