"""Render search hits for the CLI / MCP."""

from __future__ import annotations


def format_results(hits: list[dict], query: str) -> str:
    if not hits:
        return f'No results for "{query}".'
    lines = [f'{len(hits)} result(s) for "{query}":', ""]
    for h in hits:
        title = h.get("thread_title") or f"thread {h['thread_id']}"
        snippet = " ".join((h.get("snippet") or "").split())
        lines.append(
            f"[{h['thread_id']}/{h['event_id']}] {title} · {h.get('content_type') or h['event_type']}"
        )
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)
