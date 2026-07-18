"""Parsing a provider's format into the normalized message shape.

The parsers are a dependency-free island: no store, no database, no archive
config — just format knowledge. A provider plugin writes its parsing against
this module and can unit-test it with nothing else running.

## The two-step

Import is always parse-then-build. A **parser** turns a provider's raw lines
into :data:`NormalizedMessage` dicts; an **event builder** turns those into the
events archive stores. The builder is shared — :class:`DefaultEventBuilder`
handles every provider — so a plugin supplies only the parser half.

You may not need a :class:`ProviderParser` subclass at all. Most of archive's
own providers hand-build ``NormalizedMessage`` dicts and pass them straight to
:class:`DefaultEventBuilder`; subclassing buys the block-construction helpers
and registry membership, not correctness. Either path is supported.

## Preserving what you don't model

Nothing may be silently dropped. A line kind you don't model is preserved
verbatim as an ``unknown_line`` block; a block type the builder doesn't know
becomes a ``content_block`` event; an unparseable line becomes a
``parse_error`` block. All three survive a round trip, so a format you haven't
caught up with is still recoverable from the archive rather than lost.

For provider-specific extras that don't fit a block, use **annotations**: put a
dict at ``message["provider_data"]["annotations"]`` or ``block["annotations"]``
and it is copied verbatim onto the event payload. Annotations are deliberately
excluded from dedup-key computation, so enriching a message later never forks
it into a second event.

## Declaring what you know (ProviderConfig)

:class:`ProviderConfig` is a drift ledger, not a schema — it never rejects
anything. It records what your format looks like today, so that when the
provider grows a new line kind or a new field, the difference surfaces as a
warning instead of passing silently. The check exists because a new field on an
already-modeled line is the one class of loss nothing else catches: the value
rides into ``provider_data`` and is dropped at the builder seam with no error.

Set it as ``Provider.parser_config`` and archive registers it for you.

For a harness sharing another provider's format, derive rather than restate::

    MYHARNESS_CONFIG = CLAUDE_CODE_CONFIG.derive(
        "myharness",
        expected_unmodeled_line_types={"myharness_meta"},
        known_line_fields={"assistant": {"session_id"}},
        known_message_fields={"cost"},
    )

:meth:`ProviderConfig.derive` unions into the parent's ledgers, so you declare
only your additions and stay current with the parent's.
"""

from __future__ import annotations

from .._thread_import import DefaultEventBuilder, EventBuilder, ThreadEvent
from .._thread_import.event_builder import compute_content_hash, compute_dedup_key
from .._thread_import.parsers import (
    CHATGPT_CONFIG,
    CLAUDE_CODE_CONFIG,
    CLAUDE_CONFIG,
    ChatGPTParser,
    ClaudeCodeParser,
    ClaudeParser,
    CodeBlock,
    ContentBlock,
    FieldMapping,
    FileBlock,
    ImageBlock,
    NormalizedMessage,
    ProviderConfig,
    ProviderParser,
    SystemContextBlock,
    TextBlock,
    ThinkingBlock,
    ThinkingExpectation,
    ToolResultBlock,
    ToolUseBlock,
    get_parser,
    get_provider_config,
    register_parser,
    register_provider_config,
    registered_parsers,
    registered_providers,
)
from .._thread_import.timestamps import parse_timestamp, parse_timestamp_iso
from .._thread_import.tool_names import normalize_tool_name

__all__ = [
    # Parsing
    "ProviderParser",
    "NormalizedMessage",
    "ContentBlock",
    "FieldMapping",
    # Block types
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ImageBlock",
    "FileBlock",
    "CodeBlock",
    "SystemContextBlock",
    # Event building
    "EventBuilder",
    "DefaultEventBuilder",
    "ThreadEvent",
    "compute_content_hash",
    "compute_dedup_key",
    # Configuration
    "ProviderConfig",
    "ThinkingExpectation",
    "CLAUDE_CODE_CONFIG",
    "CLAUDE_CONFIG",
    "CHATGPT_CONFIG",
    "get_provider_config",
    "register_provider_config",
    "registered_providers",
    # Built-in parsers, for a format that shares one
    "ClaudeCodeParser",
    "ClaudeParser",
    "ChatGPTParser",
    "get_parser",
    "register_parser",
    "registered_parsers",
    # Utilities
    "parse_timestamp",
    "parse_timestamp_iso",
    "normalize_tool_name",
]
