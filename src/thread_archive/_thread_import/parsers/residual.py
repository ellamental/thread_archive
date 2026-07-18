"""Unmodeled-field preservation: the residual of a modeled source line.

A parser reads a line kind it models and extracts the fields it knows. Any key
the provider grew since the field ledger was written rides only in
``provider_data["line"]`` — in-memory parse state the builder never persists —
so without intervention its value is dropped at the builder seam. The
*residual* is exactly that difference: the raw line's keys (and its nested
``message`` object's keys) minus the provider's ``known_line_fields`` /
``known_message_fields`` ledgers.

:func:`annotate_unmodeled_fields` copies each message's residual into
``provider_data["annotations"]["unmodeled"]``, the sanctioned extras channel
the event builder persists verbatim onto the line's anchor event. Annotations
are outside dedup identity, so preserving a residual never forks an event, and
a later backfill can enrich already-stored events with the same shape.

:func:`unmodeled_residual` is the single computation both this module and
``TypeValidator`` run — the validator warns on the residual's *names*, this
preserves its *values* — so the warning and the preservation can never
disagree about what counts as unmodeled. A field's endgame is still a ledger
decision: model it, annotate it explicitly, or add it to the ledger as a
conscious drop. Until someone decides, the residual keeps the value.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .base import NormalizedMessage
from .config import get_provider_config

#: The annotations key residuals are preserved under.
UNMODELED_KEY = "unmodeled"


def unmodeled_residual(
    msg: NormalizedMessage, config: Any
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(line_residual, message_residual)`` for one message — ``({}, {})`` when clean.

    Scope mirrors the ledgers exactly: an empty ``known_line_fields`` (or a role
    absent from it) disables the line check; an empty ``known_message_fields``
    disables the message check. A provider that has not declared ledgers gets no
    residual — there is no baseline to diff against.
    """
    if not config.known_line_fields:
        return {}, {}
    line = (msg.get("provider_data") or {}).get("line")
    if not isinstance(line, dict):
        return {}, {}
    known = config.known_line_fields.get(msg.get("role", ""))
    line_residual = (
        {k: v for k, v in line.items() if k not in known} if known else {}
    )
    message_residual: Dict[str, Any] = {}
    if config.known_message_fields:
        inner = line.get("message")
        if isinstance(inner, dict):
            message_residual = {
                k: v
                for k, v in inner.items()
                if k not in config.known_message_fields
            }
    return line_residual, message_residual


def annotate_unmodeled_fields(
    messages: List[NormalizedMessage], provider: str
) -> int:
    """Preserve every message's unmodeled-field residual as an annotation.

    Writes ``provider_data["annotations"]["unmodeled"] = {"line": {...},
    "message": {...}}`` (only the non-empty halves) on each message with a
    residual, replacing any prior value of that key — the residual is derived
    state, recomputed per parse. Returns the number of messages annotated.
    Unknown provider → 0 (no ledger, nothing to diff).
    """
    try:
        config = get_provider_config(provider)
    except KeyError:
        return 0
    annotated = 0
    for msg in messages:
        line_residual, message_residual = unmodeled_residual(msg, config)
        if not line_residual and not message_residual:
            continue
        residual: Dict[str, Any] = {}
        if line_residual:
            residual["line"] = line_residual
        if message_residual:
            residual["message"] = message_residual
        provider_data = msg.setdefault("provider_data", {})
        annotations = provider_data.setdefault("annotations", {})
        annotations[UNMODELED_KEY] = residual
        annotated += 1
    return annotated
