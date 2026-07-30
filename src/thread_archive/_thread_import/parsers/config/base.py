"""
Provider configuration dataclasses.

ProviderConfig captures provider-specific characteristics that affect
parsing and validation. This moves configuration out of base.py and
makes it owned by each provider.
"""

from dataclasses import dataclass, field, replace
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
        persist_branch_metadata: Whether each event stores its parent link and
            active-path flag as ``payload["branch"]``. Distinct from
            ``has_branching``, which describes the *format*: a format can have
            branches without the archive needing to persist them. Set it when the
            source is delivered as a **tree** whose flat event order can't recover
            which reply followed which prompt (an account export of regenerations
            and edits) — there, the parent link is the only way to rebuild the
            conversation from truth. A source whose log is an append-only
            transcript already implies its chain by order, and a parent id on
            every event would be noise.
        expected_roles: Valid role values for this provider
        expected_block_types: Valid content block types for this provider
        expected_unmodeled_line_types: Source line kinds the parser deliberately
            preserves verbatim (as ``unknown_line`` blocks) without modeling —
            known bookkeeping, not drift. An ``unknown_line`` block whose
            ``line_type`` is NOT in this set is the drift signal: a line kind
            the provider added that the parser has never seen.
        timestamp_format: How timestamps are formatted in exports
            - "unix_seconds": Unix timestamp in seconds
            - "unix_millis": Unix timestamp in milliseconds
            - "iso": ISO 8601 string
            - "mixed": Can be any of the above (auto-detect)
        known_line_fields: Field-level drift ledger — role → the set of top-level
            keys the parser knows about on that role's raw source line
            (``provider_data["line"]``). The type/role/line-type ledgers above
            cannot see a NEW FIELD appear on an already-modeled line. Any line
            key outside the role's set warns, and the import seam preserves its
            value under the anchor event's ``annotations["unmodeled"]`` (see
            ``parsers.residual``) until someone decides its endgame: model it,
            annotate it, or add it here as a conscious drop. Empty dict (or a
            role absent from it) disables both the check and the preservation.
        known_message_fields: Same ledger for the nested ``line["message"]``
            object's keys, role-independent. Empty set disables.
    """

    provider_name: str
    thinking_expectation: ThinkingExpectation = "optional"
    thinking_exempt_models: Set[str] = field(default_factory=set)
    has_branching: bool = False
    has_parent_references: bool = False
    persist_branch_metadata: bool = False
    expected_roles: Set[str] = field(
        default_factory=lambda: {"user", "assistant", "system"}
    )
    expected_block_types: Set[str] = field(default_factory=set)
    expected_unmodeled_line_types: Set[str] = field(default_factory=set)
    timestamp_format: Literal["unix_seconds", "unix_millis", "iso", "mixed"] = "mixed"
    known_line_fields: Dict[str, Set[str]] = field(default_factory=dict)
    known_message_fields: Set[str] = field(default_factory=set)

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

    def derive(
        self,
        provider_name: str,
        *,
        expected_roles: Optional[Set[str]] = None,
        expected_block_types: Optional[Set[str]] = None,
        expected_unmodeled_line_types: Optional[Set[str]] = None,
        known_line_fields: Optional[Dict[str, Set[str]]] = None,
        known_message_fields: Optional[Set[str]] = None,
        **overrides: object,
    ) -> "ProviderConfig":
        """A config for another provider that shares this one's parser.

        A harness that writes another provider's transcript shape reuses that
        provider's parser but is its own source, with its own drift ledgers: it
        may carry extra line types and extra fields the parent never emits.
        Registering those on the parent's config would blame the parent for a
        field it will never grow and blind the parent's ledger to that field
        appearing for real.

        The five ledger arguments **union** into the parent's sets rather than
        replacing them — a derived provider is the parent's shape plus its own
        additions. ``known_line_fields`` unions per role. Anything else about the
        parent (thinking expectation, timestamp format, branching) carries over
        unchanged unless named in ``overrides``.
        """
        merged_line_fields = {role: set(keys) for role, keys in self.known_line_fields.items()}
        for role, keys in (known_line_fields or {}).items():
            merged_line_fields[role] = merged_line_fields.get(role, set()) | set(keys)
        return replace(
            self,
            provider_name=provider_name,
            expected_roles=self.expected_roles | set(expected_roles or ()),
            expected_block_types=self.expected_block_types | set(expected_block_types or ()),
            expected_unmodeled_line_types=(
                self.expected_unmodeled_line_types | set(expected_unmodeled_line_types or ())
            ),
            known_line_fields=merged_line_fields,
            known_message_fields=self.known_message_fields | set(known_message_fields or ()),
            **overrides,  # type: ignore[arg-type]
        )


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
    # The export is a conversation tree: a node can have several children
    # (regenerations), so flat event order can't recover which reply followed
    # which prompt, nor which branch is the active one.
    persist_branch_metadata=True,
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
    # claude.ai exports include thinking blocks for reasoning models but not
    # for non-reasoning ones — no per-model rule is reliable, so no validation.
    thinking_expectation="optional",
    thinking_exempt_models=set(),
    has_branching=True,  # edit/regenerate tree via parent_message_uuid
    has_parent_references=True,
    persist_branch_metadata=True,  # same tree shape as ChatGPT's export
    expected_roles={"user", "assistant", "human"},  # Claude uses "human" for user
    expected_block_types={
        "text",
        "thinking",  # reasoning-model conversations export their thinking blocks
        "tool_use",
        "tool_result",
        "system_metadata",
        "flag",  # safety marker blocks, preserved raw
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
        "attachment",  # non-queued_command attachment sub-kinds, preserved as hidden system records
        "model_change",  # a manual /model switch, preserved so the archive can show it
        "pr_link",  # the pull request a session is working on (folded into event_prs)
        "unknown_line",  # verbatim preservation of an unmodeled line kind (see below)
    },
    # Line kinds the parser knowingly preserves verbatim without modeling:
    # session-state bookkeeping (titles are consumed separately by the importer's
    # _titles helpers). An unknown_line block outside this set is real drift.
    expected_unmodeled_line_types={
        "last-prompt",  # resume bookkeeping: copy of the latest prompt + leaf uuid
        "ai-title",  # the auto-titler's current title (importer reads it for the thread title)
        "custom-title",  # a user rename (wins over ai-title; importer reads it too)
        "mode",  # permission-mode switches (normal/plan/…)
        "permission-mode",  # newer sibling of "mode": current permission mode
        "file-history-delta",  # file-backup bookkeeping, sibling of file-history-snapshot
        "started",  # Workflow orchestration journal: a subagent began (keyed by cache hash)
        "result",  # Workflow orchestration journal: a subagent finished, carrying its result
    },
    timestamp_format="iso",
    # Field-level drift ledger: every top-level key observed on real user /
    # assistant lines under ~/.claude/projects. A key outside these sets is
    # FUTURE drift — a field Claude Code grew that the parser has never seen —
    # and warns via TypeValidator. A harness that shares this parser adds its
    # own extra keys through ``ProviderConfig.derive``, not here, so this ledger
    # stays a statement about Claude Code alone. Keep sorted.
    known_line_fields={
        "user": {
            # queued_command attachment lines are rebuilt as user-role messages,
            # so their one extra key is known here too.
            "agentId",  # subagent provenance: thread-level, see the assistant note
            "attachment",
            "cwd",
            "entrypoint",
            "gitBranch",
            "imagePasteIds",
            "interruptedByShutdown",  # the user turn was cut short by a shutdown
            "isCompactSummary",
            "isMeta",
            "isSidechain",
            "isVisibleInTranscriptOnly",
            "mcpMeta",
            "message",
            "origin",
            "parentUuid",
            "permissionMode",
            "promptId",
            "promptSource",
            "sessionId",
            "slug",
            "sourceToolAssistantUUID",
            "sourceToolUseID",
            "thinkingMetadata",
            "timestamp",
            "todos",
            "toolDenialKind",
            # Marks the tool result that ended the agent's turn — in practice a
            # StructuredOutput result the schema accepted. Carried, not stored:
            # it restates what the persisted blocks already say, since the flag
            # is present exactly when the result's tool_use is StructuredOutput
            # and the result is not an error (a schema rejection keeps the turn
            # going, and is already modeled as tool_execution_error). Persisting
            # it would duplicate that seam and give it a second place to drift.
            "toolEndsTurn",
            "toolUseResult",
            "type",
            "userType",
            "uuid",
            "version",
        },
        "assistant": {
            # agentId / attributionAgent appear only on subagent transcripts, and
            # both are constant across a whole file — they identify the agent the
            # transcript IS, not anything about the message. So they are persisted
            # once per thread (source_metadata's agent_id / agent_type, stamped by
            # the importer's _cc_origin_metadata) rather than annotated onto every
            # message, which would write the same constant onto every event.
            "agentId",
            "apiErrorStatus",
            "attributionAgent",
            "attributionMcpServer",
            "attributionMcpTool",
            "attributionSkill",
            "cwd",
            "effort",
            "entrypoint",
            "error",
            "gitBranch",
            "isApiErrorMessage",
            "isSidechain",
            "message",
            "parentUuid",
            "requestId",
            "sessionId",
            "slug",
            "supersedesUuids",
            "timestamp",
            "type",
            "userType",
            "uuid",
            "version",
        },
    },
    # Known keys of line["message"], role-independent (union of user +
    # assistant message objects). Keep sorted.
    known_message_fields={
        "container",
        "content",
        "context_management",
        "diagnostics",
        "id",
        "model",
        "role",
        "stop_details",
        "stop_reason",
        "stop_sequence",
        "type",
        "usage",
    },
)


# Registry of all provider configs. Providers whose parser lives in this island
# are seeded here; every other provider — a delegating source, a plugin — pushes
# its config in via :func:`register_provider_config`. Registration is a push and
# never a pull because this island imports nothing from the rest of the package:
# reaching outward for a provider registry would couple the parsers to the store
# and break the isolation `test_vendor_thread_import` locks.
_PROVIDER_CONFIGS: Dict[str, ProviderConfig] = {
    "chatgpt": CHATGPT_CONFIG,
    "claude": CLAUDE_CONFIG,
    "claude-code": CLAUDE_CODE_CONFIG,
}


def register_provider_config(config: ProviderConfig) -> None:
    """Register ``config`` under its own ``provider_name``, replacing any prior one.

    Idempotent: re-registering the same provider overwrites, so a repeated
    registry load is a no-op rather than an error.
    """
    _PROVIDER_CONFIGS[config.provider_name] = config


def registered_providers() -> list[str]:
    """Every provider name with a registered config, sorted."""
    return sorted(_PROVIDER_CONFIGS)


def get_provider_config(provider: str) -> ProviderConfig:
    """Get the configuration for a provider.

    Args:
        provider: Provider name (chatgpt, claude, claude-code, …)

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
