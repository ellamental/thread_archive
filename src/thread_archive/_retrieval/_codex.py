"""How the reader renders codex's preserved content blocks.

The codex importer is an archivist: every line kind it doesn't model survives as a
``content_block`` event whose ``data`` carries the raw payload under a ``codex_<kind>``
block type. Deciding what a reader should actually *see* is left to here.

Codex writes its transcript twice. ``event_msg.user_message`` / ``event_msg.agent_message``
is the canonical stream the importer models into user turns and text blocks; the
``response_item.message`` API history repeats every one of those turns verbatim, and the
line stream interleaves both with turn lifecycle, token telemetry, and sandbox config.
Rendering the preserved blocks as-is therefore prints each assistant reply twice and
buries it in machinery. Each block gets one of three fates:

* **machinery** — lifecycle, telemetry, environment, and the ``event_msg`` mirrors of a
  tool call already modeled as tool_use/tool_result. Hidden.
* **echo** — text the modeled path already renders. Hidden, matched on content rather
  than kind, so a duplicate can't slip through under a kind this module hasn't met.
* **content** — a developer/system prompt, the context codex injects as a user turn, a
  web or tool search, a compaction or abort marker. Rendered under its own label.

Hiding is a rendering decision, not a deletion: every raw payload stays in the event log.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Container, Optional

_PREFIX = "codex_"

# An unmodeled kind falls back to a JSON dump of its payload. Cap it — a tool_search
# output carries every tool description the model was offered.
_RAW_CAP = 2000

# Kinds that say nothing about the conversation. ``task_complete`` carries a
# ``last_agent_message`` and ``item_completed`` a structured mirror of the turn's own
# output; ``mcp_tool_call_end`` and ``patch_apply_end`` restate a function_call the
# importer already models as a tool_use/tool_result pair, and ``web_search_end``
# restates its web_search_call.
_MACHINERY = frozenset({
    "task_started",
    "task_complete",
    "token_count",
    "turn_context",
    "world_state",
    "item_completed",
    "mcp_tool_call_end",
    "patch_apply_end",
    "web_search_end",
})


def codex_kind(block_type: Any) -> Optional[str]:
    """The codex line/payload kind behind a ``codex_<kind>`` block type, else None."""
    if isinstance(block_type, str) and block_type.startswith(_PREFIX):
        return block_type[len(_PREFIX):] or None
    return None


def _message(raw: dict) -> tuple[str, str]:
    """A ``response_item.message`` — codex's API-history copy of a turn. Real content
    only when it has no ``event_msg`` twin: the developer/system prompt, and the
    environment and instruction context codex injects wearing the user's role."""
    role = raw.get("role")
    label = f"{role} message" if isinstance(role, str) and role else "message"
    parts: list[str] = []
    for chunk in raw.get("content") or []:
        if not isinstance(chunk, dict):
            continue
        text = chunk.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
        elif chunk.get("type") == "input_image":
            parts.append("[image]")  # the payload is a base64 data URI
    return label, "\n".join(parts).strip()


def _web_search(raw: dict) -> tuple[str, str]:
    action = raw.get("action")
    action = action if isinstance(action, dict) else {}
    bits = [str(action.get("type") or "search")]
    for key in ("query", "url"):
        value = action.get(key) or raw.get(key)
        if value:
            bits.append(str(value))
    return "web search", " · ".join(bits)


def _tool_search_call(raw: dict) -> tuple[str, str]:
    args = raw.get("arguments")
    args = args if isinstance(args, dict) else {}
    return "tool search", str(args.get("query") or "")


def _tool_search_output(raw: dict) -> tuple[str, str]:
    names: list[str] = []
    for tool in raw.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            names.append(str(tool["name"]))
    return "tool search result", ", ".join(names)


def _compacted(raw: dict) -> tuple[str, str]:
    # ``replacement_history`` — the synthetic history codex swapped in — is preserved in
    # the payload but far too large to render; the operator-facing part is the message.
    return "context compacted", str(raw.get("message") or "")


def _context_compacted(_raw: dict) -> tuple[str, str]:
    return "context compacted", ""


def _turn_aborted(raw: dict) -> tuple[str, str]:
    return "turn aborted", str(raw.get("reason") or "")


_RENDERERS: dict[str, Callable[[dict], tuple[str, str]]] = {
    "message": _message,
    "web_search_call": _web_search,
    "tool_search_call": _tool_search_call,
    "tool_search_output": _tool_search_output,
    "compacted": _compacted,
    "context_compacted": _context_compacted,
    "turn_aborted": _turn_aborted,
}


def _raw_dump(kind: str, raw: dict) -> tuple[str, str]:
    """A kind this module has never met — a future codex line type. Show its payload
    as readable JSON rather than dropping it or smashing it into one line."""
    try:
        text = json.dumps(raw, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(raw)
    if len(text) > _RAW_CAP:
        text = text[:_RAW_CAP] + "… (truncated)"
    return kind.replace("_", " "), text


def render_codex_block(
    kind: str, data: Any, rendered_text: Container[str]
) -> Optional[tuple[str, str]]:
    """``(label, text)`` for a preserved codex block, or None when the reader hides it.

    ``rendered_text`` holds the stripped text of every turn the modeled path already
    shows; a block matching one of them is codex's duplicate transcript, not new content.
    """
    if kind in _MACHINERY:
        return None
    raw = data.get("raw") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    renderer = _RENDERERS.get(kind)
    label, text = renderer(raw) if renderer else _raw_dump(kind, raw)
    if text and text.strip() in rendered_text:
        return None
    return label, text
