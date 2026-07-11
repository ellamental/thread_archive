"""
Pluggable validators for the parser pipeline.

Validators check messages against rules and report errors/warnings.
Each validator focuses on a single concern for testability.
"""

import logging

from ..config import ProviderConfig, get_provider_config
from .base import BaseValidator, ValidationContext
from .content import ContentValidator
from .referential import ReferentialIntegrityValidator
from .thinking import ThinkingBlockValidator
from .types import TypeValidator

logger = logging.getLogger(__name__)

__all__ = [
    "ValidationContext",
    "BaseValidator",
    "ThinkingBlockValidator",
    "ReferentialIntegrityValidator",
    "TypeValidator",
    "ContentValidator",
    "validate_messages",
]

# Run order: content/type first (per-message shape), then thinking/referential
# (aggregate). All are read-only — they only collect issues into the context.
_VALIDATORS = (
    ContentValidator,
    TypeValidator,
    ThinkingBlockValidator,
    ReferentialIntegrityValidator,
)


# Validators with no cross-message / conversation-aggregate state — safe to run
# on a *partial* conversation (an incremental import batch) without false
# positives. TypeValidator only scans each block/role against the known set, so
# it catches provider format drift (a new block type / role) on any slice.
_BATCH_SAFE_VALIDATORS = (TypeValidator,)


def validate_messages(messages, conversation_id, source_provider, *, strict=False, batch_safe=False):
    """Run the validators over one parsed/assembled conversation and return the
    ValidationContext (``.errors`` / ``.warnings``). Pure — no I/O; the caller
    decides what to do with the issues (we log them, never reject).

    An unknown provider falls back to a permissive default config, so the
    universal checks (unknown block types, missing content/timestamps) still run
    everywhere; provider-specific checks (thinking expectation, expected roles)
    only kick in for the configured providers.

    ``batch_safe=True`` runs only the validators with no conversation-aggregate
    state (just format-drift type checks), so it's correct on a *partial*
    conversation — used by the incremental watcher path, where each poll imports
    only a slice and the aggregate checks ("0% thinking", "no user messages")
    would otherwise false-fire.
    """
    try:
        config = get_provider_config(source_provider)
    except KeyError:
        config = ProviderConfig(provider_name=source_provider)
    context = ValidationContext(
        conversation_id=conversation_id or "",
        source_provider=source_provider,
        strict=strict,
    )
    for validator_cls in (_BATCH_SAFE_VALIDATORS if batch_safe else _VALIDATORS):
        try:
            validator_cls(config, strict=strict).validate(messages, context)
        except Exception:  # one bad validator must never break an import
            # ...but it must never be silent either: surface the crash loudly
            # (with traceback) while still letting the import proceed.
            logger.exception(
                "Validator %s crashed for conversation %r (provider %r); skipping it",
                validator_cls.__name__,
                conversation_id,
                source_provider,
            )
    return context
