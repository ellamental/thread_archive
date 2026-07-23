"""Override patch for the claude-code provider — scaffolded by `thread_archive fix-import`.

Shadows the built-in claude-code provider (declared under "providers" in the archive
home's config.json). Start from the built-in descriptor and replace only what
the fix changes. Import from thread_archive.provider / .parse — with one
sanctioned exception: a patch may reach into thread_archive internals it is
fixing (a patch is temporary by design; retirement on the next core release
bounds the exposure).

## The drift

Claude Code 2.1.x (first seen on this machine 2026-07-23) grew new top-level
fields on already-modeled line kinds:

- ``session_id`` (snake_case, distinct from the long-standing camelCase
  ``sessionId``) on user/assistant/attachment/system lines. Usually equal to
  ``sessionId``; on a resumed/forked session it instead carries the ORIGIN
  session's id — real lineage information, so its value must keep flowing,
  not be consciously dropped.
- ``sessionKind`` (observed value: ``"bg"``) on the same line kinds.
- ``interruptedMessageId`` and ``classifierMetaLines`` on user lines (rare).

## The fix (ledger + explicit annotation)

Two reach-ins, applied at module import so every path sees them — the plain
``parser_config`` override alone only takes effect when the provider registry
loads this plugin, which the scaffold's own test harness (isolated tmp home,
no plugin declared) never does:

1. The built-in ``CLAUDE_CODE_CONFIG.known_line_fields`` sets are extended IN
   PLACE. Every registration path (the parser island's seed dict, a registry
   rebuild, ``_ensure_provider_configs``) re-registers that same object, so
   the extension survives re-registration instead of racing it.
2. The parser module's ``_USER_LINE_ANNOTATIONS`` / ``_ASSISTANT_LINE_ANNOTATIONS``
   mappings are extended so the new fields' VALUES ride the sanctioned
   annotations channel (``_line_annotations`` reads the module globals at call
   time, and the importer always instantiates the built-in parser class).
   Ledgering alone would silence the warning but drop the values at the
   builder seam.

Historical events keep the values under ``annotations["unmodeled"]`` where the
residual path already preserved them; events parsed after this patch carry them
as first-class annotations (``session_id``, ``session_kind``, ...).
"""

from dataclasses import replace

from thread_archive._thread_import.parsers import claude_code as _claude_code
from thread_archive._thread_import.parsers.config.base import CLAUDE_CODE_CONFIG
from thread_archive.provider import builtin

BASE = builtin("claude-code")

_USER_FIELDS = frozenset(
    {"classifierMetaLines", "interruptedMessageId", "sessionKind", "session_id"}
)
_ASSISTANT_FIELDS = frozenset({"sessionKind", "session_id"})

# Annotation keys follow the existing convention (camelCase → snake_case,
# names kept faithful — no semantic renaming).
_USER_ANNOTATIONS = (
    ("classifierMetaLines", "classifier_meta_lines"),
    ("interruptedMessageId", "interrupted_message_id"),
    ("sessionKind", "session_kind"),
    ("session_id", "session_id"),
)
_ASSISTANT_ANNOTATIONS = (
    ("sessionKind", "session_kind"),
    ("session_id", "session_id"),
)

# Reach-in 1: extend the live ledger object (idempotent — set union).
CLAUDE_CODE_CONFIG.known_line_fields["user"] |= _USER_FIELDS
CLAUDE_CODE_CONFIG.known_line_fields["assistant"] |= _ASSISTANT_FIELDS

# Reach-in 2: extend the annotation mappings (guarded — idempotent on re-exec).
for _pair in _USER_ANNOTATIONS:
    if _pair not in _claude_code._USER_LINE_ANNOTATIONS:
        _claude_code._USER_LINE_ANNOTATIONS += (_pair,)
for _pair in _ASSISTANT_ANNOTATIONS:
    if _pair not in _claude_code._ASSISTANT_LINE_ANNOTATIONS:
        _claude_code._ASSISTANT_LINE_ANNOTATIONS += (_pair,)

# The declarative half: the descriptor the registry loads on activation. The
# derive() unions with the (already-extended) base, so content is identical to
# reach-in 1 — this keeps the override honest for tooling that reads the
# descriptor rather than the island's registry.
PROVIDER = replace(
    BASE,
    parser_config=CLAUDE_CODE_CONFIG.derive(
        "claude-code",
        known_line_fields={
            "user": set(_USER_FIELDS),
            "assistant": set(_ASSISTANT_FIELDS),
        },
    ),
)
