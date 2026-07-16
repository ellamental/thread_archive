"""thread_read: reconstruct a readable conversation transcript from events.

The event log is the source of truth; a conversation is rebuilt by walking a
thread's events in order. The importer's ``DefaultEventBuilder`` emits granular
per-block events (text_complete / thinking_complete / tool_use_complete /
tool_execution_*) plus lifecycle/summary events (api_request_*, stream_completed)
— we render from the granular events and skip the lifecycle ones.

:func:`read_thread` is the string transcript surface (CLI / MCP). It mirrors the
monorepo ``thread_read`` contract: a ``mode`` view knob (user / chat / full),
turn-based pagination (``limit`` / ``offset`` / ``after_event``), focused reads
around a search-result event (``around_event`` / ``context_turns``), a per-chunk
``max_chars`` budget with a CHUNKED footer, and ``summary`` for the summary views
(true/'toc' = compact TOC; 'short' / 'indexed' = the stored thread summaries). The
default view is ``user`` — only the user turns, the cheap signal — exactly as the
monorepo defaults. Tool *results* are never rendered (the transcript shows tool
calls, not their output), also matching the monorepo. :func:`read_thread_structured`
is the render-friendly sibling for the web viewer (typed blocks, results included).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Container, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .._store import Event, Thread, use_session
from .._truth.layout import is_redacted_payload
from ._codex import codex_kind, render_codex_block
from ._extract import _block_search_text

# Lifecycle / duplicate-summary events that carry no standalone transcript text.
_SKIP_TYPES = frozenset({
    "api_request_started",
    # Its content renders via text_complete/thinking_complete — the importer's twins,
    # or the ones _absorb_stream_deltas synthesizes for live-capture streams.
    "api_request_completed",
    "stream_completed",
    "tool_loaded",
    # Absorbed into synthesized completes by _absorb_stream_deltas; a stray one
    # (no api_call_id at all) is token noise, never a renderable block.
    "text_delta",
    "thinking_delta",
    # Duplicates tool_use_complete (same tool_name/input, minus the outcome).
    "tool_execution_started",
    # A dedup marker — its content is the duplicate_of event, already rendered.
    "archived_duplicate",
    # Hook-injected context (sidecar lines) — skipped on the token-budgeted string
    # path only; the structured (web) path renders these as hook blocks.
    "hook_context",
    # Pure session bookkeeping the source parser marks visually-hidden — not content.
    # Rendering them (as "QUEUE_OPERATION"/"FILE_SNAPSHOT" boxes) is just noise.
    "queue_operation",
    "file_snapshot",
    # Skipped on the string path; the structured path surfaces hook_progress
    # firings (which hooks ran, next to the tool calls they ran on) as markers.
    "progress",
})


def _payload(ev: Event) -> dict:
    p = ev.payload if isinstance(ev.payload, dict) else json.loads(ev.payload)
    if is_redacted_payload(p):
        # Redacted content renders as a visible placeholder, never a silent gap —
        # the text keys cover every renderer that reads one.
        return {"content": "[redacted]", "text": "[redacted]", **p}
    return p


def _unknown_payload_text(payload: dict) -> str:
    """Best-effort short text for an event type the reader doesn't model — so an
    unrecognized event is *surfaced*, never silently dropped. Prefers obvious text
    keys, else a compact JSON dump (provider_data omitted; capped)."""
    for k in ("content", "text", "output", "error"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v
    try:
        return json.dumps(
            {k: v for k, v in payload.items() if k != "provider_data"}, default=str
        )[:1000]
    except Exception:  # noqa: BLE001 — rendering must never raise
        return ""


def _attachment_raw(p: dict) -> Optional[dict]:
    """The raw Claude Code attachment preserved on an attachment context_summary
    event (``payload.provider_data.line.attachment``), or None when the event
    isn't one. The importer keeps the whole attachment; the placeholder in
    ``content`` ("[attachment: …]") is just its label."""
    if p.get("system_type") != "attachment":
        return None
    line = (p.get("provider_data") or {}).get("line")
    att = (line or {}).get("attachment") if isinstance(line, dict) else None
    return att if isinstance(att, dict) else None


def _attachment_text(att: dict) -> str:
    """Best-effort text of an attachment's injected content: ``content`` as a
    string or list of strings (a hook can inject several), else a compact JSON
    dump of the attachment's own fields — so the viewer always has something
    real to show, never just the placeholder."""
    c = att.get("content")
    if isinstance(c, str) and c.strip():
        return c
    if isinstance(c, list):
        parts = [x for x in c if isinstance(x, str) and x.strip()]
        if parts:
            return "\n\n".join(parts)
        if c:
            # Non-string items (e.g. a todo reminder's item dicts) — dump them whole
            # rather than reducing the attachment to its bare counters.
            try:
                return json.dumps(c, indent=2, default=str)[:_TOOL_OUTPUT_CAP]
            except Exception:  # noqa: BLE001 — rendering must never raise
                pass
    try:
        rest = {k: v for k, v in att.items() if k not in ("type", "content")}
        return json.dumps(rest, indent=2, default=str)[:_TOOL_OUTPUT_CAP] if rest else ""
    except Exception:  # noqa: BLE001 — rendering must never raise
        return ""


def _hook_name(att: dict) -> str:
    return att.get("hookName") or att.get("hookEvent") or "hook"


def _rendered_text(events: list[Event]) -> set[str]:
    """The text of every turn the modeled path already renders in this thread.

    Codex records its transcript twice — once as the ``event_msg`` stream the importer
    models, once as ``response_item.message`` API history preserved verbatim — so the
    reader needs to know a block's content is already on screen. Matching on text rather
    than kind means a duplicate can't slip through under a kind we haven't met."""
    seen: set[str] = set()
    for ev in events:
        if ev.event_type in _USER_TYPES:
            key = "content"
        elif ev.event_type == "text_complete":
            key = "text"
        else:
            continue
        value = _payload(ev).get(key)
        if isinstance(value, str) and value.strip():
            seen.add(value.strip())
    return seen


def _content_block_view(p: dict, rendered_text: Container[str]) -> Optional[tuple[str, str]]:
    """``(label, text)`` for a preserved ``content_block`` event, or None to hide it.

    Only codex blocks are ever hidden — see :mod:`._codex`. Every other provider's
    preserved block renders under its own block type, flattened to its readable text."""
    block_type = p.get("block_type") or "block"
    kind = codex_kind(block_type)
    if kind is not None:
        return render_codex_block(kind, p.get("data"), rendered_text)
    return block_type, _block_search_text(p.get("data"))


def resolve_thread_ref(s: Session, ref: int | str) -> Optional[int]:
    """Resolve a thread reference to the archive's integer thread id.

    ``ref`` is either the archive's own integer thread id (the primary key) or a
    provider **session id** — the uuid/source_id a tool like claude-code knows a
    conversation by. An integer (or all-digit) ref resolves as a primary-key lookup
    first, preserving the original ``thread_id`` contract exactly; anything that
    isn't an existing PK resolves as a session id via the shared
    :func:`thread_archive._store.resolve.resolve_session_source_id` — the
    ``Thread.source_id`` ∪ ``ImportState`` union the web viewer's
    ``resolve_archive_link`` also uses. The union matters: a compaction
    continuation's session uuid exists only in ``ImportState`` (its events merge
    into the original thread, but its watermark is its own), and that uuid is
    exactly what an agent inside the continued session holds. None when nothing
    matches."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        tid = int(ref)
        if s.get(Thread, tid) is not None:
            return tid
        # A digit ref that isn't a PK may still be a numeric provider session id
        # (e.g. grok), so fall through to source_id resolution.
    from .._store import resolve_session_source_id

    return resolve_session_source_id(s, str(ref))


# Default per-read character budget for the budgeted "view" read (the thread_read
# tool + CLI). ~48k chars ≈ 12-15k tokens — under the MCP output cap with headroom,
# big enough that most threads read in one chunk; larger threads come back chunked
# with a footer naming the next offset. Mirrors the monorepo's DEFAULT_READ_CHAR_BUDGET.
DEFAULT_READ_CHAR_BUDGET = 48000

# A context-compaction continuation opens with this sentinel as its first user
# message; it becomes a chunk boundary (a short placeholder, not the huge summary).
_COMPACTION_PREFIX = "This session is being continued from a previous conversation"

_USER_TYPES = frozenset({"user_message_sent", "thread_message_sent"})

# Grok / xAI-shaped harnesses wrap the operator's actual prompt in a
# ``<user_query>`` tag and inject ``<user_info>`` / ``<environment>`` /
# ``<system-reminder>`` context around it. The importer keeps the whole turn as
# the event's truth (capture everything), so the readers surface just the query
# span for the human-facing transcript — the same span the importer uses to derive
# the thread title.
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)


def _display_user_content(content: str) -> str:
    """Unwrap a ``<user_query>…</user_query>`` span for the readable transcript.

    Both readers (the CLI/MCP string transcript and the web viewer's structured
    blocks) render the query span rather than the raw wrapper + injected context.
    The untouched original stays on the event and in the viewer's raw view; a turn
    with no query span is returned as-is."""
    match = _USER_QUERY_RE.search(content)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner
    return content


def _fmt_ts(dt) -> str:
    """ISO-ish ``YYYY-MM-DDTHH:MM:SS`` for a step/thread timestamp (seconds, no µs)."""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return str(dt).replace(" ", "T", 1)[:19]


def _fmt_hm(dt) -> str:
    """``HH:MM`` for the summary TOC time column."""
    if isinstance(dt, datetime):
        return dt.strftime("%H:%M")
    s = str(dt).replace(" ", "T", 1)
    return s[11:16] if len(s) >= 16 else s


def resolve_read_view(mode: Optional[str], user_only: Optional[bool]) -> tuple[bool, bool, bool]:
    """Map the requested view → ``(user_only, strip_tools, strip_thinking)``.

        mode=user  → user turns only             (True,  False, False)
        mode=chat  → user + assistant text       (False, True,  True)
        mode=full  → full transcript incl. tools (False, False, False)

    ``mode`` is the primary knob; ``user_only`` is a back-compat alias (True→user,
    False→full) and ``mode`` wins when both are set. Default (neither set, or an
    unrecognised mode) is ``user`` — the cheap default, matching the monorepo.
    """
    if mode is not None:
        m = mode.strip().lower()
        if m == "user":
            return True, False, False
        if m == "chat":
            return False, True, True
        if m == "full":
            return False, False, False
        # Unrecognised mode → fall through to user_only / default rather than error.
    if user_only is not None:
        return user_only, False, False
    return True, False, False


def _assistant_block(et: str, p: dict, rendered_text: Container[str]) -> Optional[dict]:
    """One assistant render block from a granular event, or None to skip.

    Tool *result* blocks (tool_execution_*) are built here but only rendered when
    the caller opts in (``tool_results=True``); they're off by default, the way the
    monorepo never shows them — but reachable, unlike the monorepo, which can't."""
    if et == "thinking_complete":
        t = p.get("text", "")
        return {"type": "thinking", "content": t} if t.strip() else None
    if et in ("tool_use_complete", "tool_use_started"):
        return {"type": "tool", "name": p.get("tool_name", "?"), "input": p.get("input") or {}}
    if et == "tool_execution_completed":
        return {"type": "tool_result", "output": str(p.get("output", ""))}
    if et == "tool_execution_error":
        return {"type": "tool_error", "error": str(p.get("error", ""))}
    if et == "text_complete":
        t = p.get("text", "")
        return {"type": "text", "content": t} if t.strip() else None
    if et == "context_summary":
        att = _attachment_raw(p)
        if att is not None:
            atype = att.get("type") or "attachment"
            if atype == "hook_additional_context":
                # A hook fired and injected content into the model's context
                # (e.g. a UserPromptSubmit hook adding system-map notes) —
                # render the hook's name and what it injected, not a placeholder.
                return {"type": "hook", "name": _hook_name(att),
                        "content": _attachment_text(att)}
            # Other preserved attachments (todo reminders, listing deltas, …) are
            # machinery — label only on this token-budgeted path.
            return {"type": "attachment", "attachment_type": atype}
        c = p.get("content", "")
        return {"type": "text", "content": f"[context summary] {c}"} if c.strip() else None
    if et == "content_block":
        view = _content_block_view(p, rendered_text)
        if view is None:
            return None
        block_type, text = view
        return {"type": "content_block", "block_type": block_type, "content": text}
    if et == "ide_context":
        return {"type": "ide_context", "context_type": p.get("context_type") or "context",
                "file_path": p.get("file_path"), "content": p.get("content", "")}
    if et == "model_change":
        # A `/model` switch — genuine context (the structured path renders it as a
        # divider); one legible line here instead of an [unknown] JSON dump.
        to = p.get("to")
        return {"type": "text", "content": f"[model → {to}]"} if to else None
    if et in _SKIP_TYPES:
        return None  # lifecycle / duplicate-summary noise — deliberately hidden
    # Any other unrecognized type: surface it rather than silently dropping it.
    return {"type": "unknown", "event_type": et, "content": _unknown_payload_text(p)}


def _slot_queued_events(events: Sequence[Event]) -> list[Event]:
    """Relocate backfilled events into their chronological slot.

    Two kinds of event can arrive with a tail-end ``Event.id`` that the id-ordered
    walk would render at the end of the thread instead of where it belongs: a
    steering message (typed mid-turn, queued, consumed as an attachment injection —
    ``payload.queued``), and a ``model_change`` marker added by re-importing an older
    thread once the ``/model`` switch capture existed. Move each to sit after the last
    other event whose ``occurred_at`` is at or before its own (max-index semantics: one
    deep event with a garbage inferred timestamp can't drag it to the top). Freshly
    captured events arrive in file order, so at most this shifts one within its own
    turn; everything else is untouched — this deliberately does NOT sort the stream,
    because bookkeeping events (``file_snapshot`` etc.) carry inferred timestamps that
    are wrong by days."""
    queued = [
        ev for ev in events
        if (ev.event_type in _USER_TYPES and _payload(ev).get("queued"))
        or ev.event_type == "model_change"
    ]
    if not queued:
        return list(events)
    rest = [ev for ev in events if ev not in queued]
    for q in sorted(queued, key=lambda e: (e.occurred_at, e.id)):
        pos = 0
        for i, ev in enumerate(rest):
            if ev.occurred_at is not None and q.occurred_at is not None \
                    and ev.occurred_at <= q.occurred_at:
                pos = i + 1
        rest.insert(pos, q)
    return rest


_DELTA_TYPES = frozenset({"text_delta", "thinking_delta"})
_COMPLETE_TWINS = frozenset({"text_complete", "thinking_complete"})


def _synthesize_completes(arc: Optional[Event], deltas: list[Event], seen_texts: set[str]) -> list:
    """Complete-twin stand-ins for one api_call that has none of its own.

    Prefer the assembled ``content_blocks`` on the ``api_request_completed``
    summary (byte-identical to the concatenated deltas, and already in model
    block order); fall back to concatenating the deltas by ``block_index`` when
    the summary is missing (a stream that died mid-call). ``seen_texts`` is the
    thread's real complete-twin texts: a block already carried by one — a doubly
    captured turn, where the file importer's twin sits under a *different*
    api_call_id than the live stream's summary — is skipped, and every emitted
    text joins the set so duplicate summaries can't synthesize twice. Each
    stand-in carries a real event id — the summary's, which is also the id the
    FTS indexer files this content under, so search hits and ``around_event``
    anchors line up."""
    from types import SimpleNamespace

    out: list = []
    ts = deltas[0].occurred_at if deltas else (arc.occurred_at if arc else None)
    blocks: list[tuple[str, str, int]] = []  # (event_type, text, anchor_id)
    if arc is not None and (_payload(arc).get("content_blocks") or None):
        for block in _payload(arc)["content_blocks"]:
            bt = block.get("type")
            if bt == "thinking" and (block.get("thinking") or "").strip():
                blocks.append(("thinking_complete", block["thinking"], arc.id))
            elif bt == "text" and (block.get("text") or "").strip():
                blocks.append(("text_complete", block["text"], arc.id))
    else:
        # No content_blocks to lean on: stitch the deltas back together per
        # block. Anchor at the summary event when one exists — that's the id
        # the FTS indexer files the stitched text under (stitch_delta_tuples),
        # so search hits land on the rendered block; a call with no summary at
        # all (killed mid-stream) anchors at its last delta.
        runs: dict = {}  # block_index -> [event_type, [texts], last_delta_id]
        for d in deltas:
            p = _payload(d)
            run = runs.setdefault(p.get("block_index", 0), [d.event_type, [], d.id])
            run[1].append(p.get("text", ""))
            run[2] = d.id
        for _, (det, texts, last_id) in sorted(runs.items()):
            text = "".join(texts)
            if text.strip():
                et = "thinking_complete" if det == "thinking_delta" else "text_complete"
                blocks.append((et, text, arc.id if arc is not None else last_id))
    for et, text, anchor in blocks:
        key = text.strip()
        if key in seen_texts:
            continue  # already rendered by a real twin elsewhere in the thread
        seen_texts.add(key)
        out.append(SimpleNamespace(
            id=anchor, event_type=et, payload={"text": text},
            occurred_at=ts, api_call_id=arc.api_call_id if arc else None,
        ))
    return out


def _absorb_stream_deltas(events: Sequence[Event]) -> list:
    """Give the renderers one assistant vocabulary across capture styles.

    File importers emit per-block ``text_complete``/``thinking_complete`` twins
    beside each ``api_request_completed``; live-capture sources (cloth, loom,
    needle, officiant, …) emit token ``text_delta``/``thinking_delta`` events —
    or nothing granular at all — and the assembled turn exists only in the
    summary's ``content_blocks``. For every api_call with no complete-twin,
    synthesize the twins — slotted just before the ``api_request_completed``,
    where an importer's twins sit — and drop the raw deltas either way. Without
    this, a live-captured thread renders as user turns with empty
    ``[ASSISTANT]`` headers in ``chat`` and as thousands of per-token lines in
    ``full``. Twin-detection is by api_call_id *and* by text (``seen_texts`` in
    :func:`_synthesize_completes`): a doubly captured thread has real twins
    under different api_call_ids than the stream's summaries."""
    twinned_calls = set()
    twin_texts: set[str] = set()
    for ev in events:
        if ev.event_type in _COMPLETE_TWINS:
            if ev.api_call_id:
                twinned_calls.add(ev.api_call_id)
            t = (_payload(ev).get("text") or "").strip()
            if t:
                twin_texts.add(t)

    def _untwinned_arc(ev) -> bool:
        return (
            ev.event_type == "api_request_completed"
            and ev.api_call_id is not None
            and ev.api_call_id not in twinned_calls
        )

    deltas: dict[str, list[Event]] = {}
    arcs = set()
    for ev in events:
        ac = ev.api_call_id
        if ev.event_type in _DELTA_TYPES and ac and ac not in twinned_calls:
            deltas.setdefault(ac, []).append(ev)
        elif ev.event_type == "api_request_completed" and ac:
            arcs.add(ac)
    if not deltas and not any(_untwinned_arc(ev) for ev in events):
        return list(events)
    # Calls whose summary never arrived (killed mid-stream) synthesize at their
    # last delta's slot instead of an arc's.
    tail_of_orphan = {evs[-1].id: ac for ac, evs in deltas.items() if ac not in arcs}
    out: list = []
    for ev in events:
        if _untwinned_arc(ev):
            out.extend(_synthesize_completes(ev, deltas.get(ev.api_call_id or "", []), twin_texts))
            out.append(ev)
            continue
        if ev.event_type in _DELTA_TYPES:
            ac = tail_of_orphan.get(ev.id)
            if ac is not None:
                out.extend(_synthesize_completes(None, deltas[ac], twin_texts))
            continue  # deltas never pass through raw
        out.append(ev)
    return out


def _build_steps(events: list[Event]) -> list[dict]:
    """Fold the granular event stream into steps (the monorepo's regroup_by_steps
    analogue): a USER message is its own step; assistant block events accumulate
    into one step that closes at each text output. Tool calls + thinking thus group
    under the step whose text they precede; trailing tools form a final step."""
    steps: list[dict] = []
    rendered_text = _rendered_text(events)
    cur: Optional[dict] = None  # open assistant step
    for ev in events:
        et = ev.event_type
        p = _payload(ev)
        if et in _USER_TYPES:
            if cur is not None:
                steps.append(cur)
                cur = None
            content = p.get("content", "")
            if not content.strip():
                continue
            steps.append({
                "role": "user",
                "id": ev.id,
                "event_ids": [ev.id],
                "ts": ev.occurred_at,
                "content": _display_user_content(content),
                "is_compaction": content.startswith(_COMPACTION_PREFIX),
            })
            continue
        if et == "message":
            # A preserved turn whose role isn't user/assistant/system (tool/developer/
            # …). Render it as its own labeled step rather than dropping it.
            if cur is not None:
                steps.append(cur)
                cur = None
            content = p.get("content", "")
            if content.strip():
                steps.append({
                    "role": p.get("role") or "message",
                    "id": ev.id,
                    "event_ids": [ev.id],
                    "ts": ev.occurred_at,
                    "content": content,
                    "is_compaction": False,
                })
            continue
        block = _assistant_block(et, p, rendered_text)
        if block is not None:
            is_result = block["type"] in ("tool_result", "tool_error")
            # A result event can land after the text that closed its step; glue it
            # back onto that step rather than spawning an orphan result-only step.
            if cur is None and is_result and steps and steps[-1]["role"] == "assistant":
                steps[-1]["blocks"].append(block)
                steps[-1]["event_ids"].append(ev.id)
            else:
                if cur is None:
                    cur = {
                        "role": "assistant", "id": ev.id, "event_ids": [],
                        "ts": ev.occurred_at, "blocks": [],
                    }
                cur["blocks"].append(block)
                cur["event_ids"].append(ev.id)
        if et == "text_complete" and cur is not None:
            steps.append(cur)
            cur = None
    if cur is not None:
        steps.append(cur)
    return steps


# Per-result transcript cap — tool output can be a megabyte; keep the head and
# flag the truncation so one result can't blow the whole char budget.
_TOOL_RESULT_CAP = 2000


def _format_tool_block(block: dict) -> str:
    """Render a tool block as ``[tool: name k=v ...]`` (each value truncated at 80)."""
    name = block.get("name", "?")
    tool_input = block.get("input") or {}
    if not tool_input:
        return f"[tool: {name}]"
    arg_strs = []
    for k, v in tool_input.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:80] + "..."
        arg_strs.append(f"{k}={v_str}")
    return f"[tool: {name} {' '.join(arg_strs)}]"


def _format_result_block(block: dict) -> str:
    """Render a tool result/error block (only when ``tool_results`` is on)."""
    if block.get("type") == "tool_error":
        err = (block.get("error", "") or "").strip()
        if len(err) > _TOOL_RESULT_CAP:
            err = err[:_TOOL_RESULT_CAP] + "… (truncated)"
        return f"[tool error] {err}"
    out = (block.get("output", "") or "").strip()
    if len(out) > _TOOL_RESULT_CAP:
        out = out[:_TOOL_RESULT_CAP] + "… (truncated)"
    return f"[result] {out}"


def _format_step(
    step: dict,
    *,
    strip_tools: bool,
    strip_thinking: bool,
    include_results: bool,
    focus_event: Optional[int] = None,
) -> str:
    """Format one step as ``[USER ...]`` / ``[ASSISTANT ...]`` text, honoring strips.

    Tool result/error blocks render only when ``include_results`` is set *and* tools
    aren't stripped (a result with its call hidden would be context-free)."""
    ts = _fmt_ts(step["ts"])
    focus = (
        f" match:{focus_event}"
        if focus_event is not None and focus_event in step.get("event_ids", [step["id"]])
        else ""
    )
    if "content" in step:  # user, or a preserved non-standard-role turn
        label = "USER" if step["role"] == "user" else step["role"].upper()
        return f"[{label} {ts} event:{step['id']}{focus}] {step['content']}"
    parts = []
    for b in step.get("blocks", []):
        bt = b.get("type")
        if bt == "tool":
            if not strip_tools:
                parts.append(_format_tool_block(b))
        elif bt in ("tool_result", "tool_error"):
            if not strip_tools and include_results:
                parts.append(_format_result_block(b))
        elif bt == "thinking":
            if not strip_thinking:
                c = b.get("content", "").strip()
                if c:
                    parts.append(f"[thinking]\n{c}\n[/thinking]")
        elif bt == "text":
            c = b.get("content", "").strip()
            if c:
                parts.append(c)
        elif bt == "ide_context":
            if not strip_tools:
                fp = b.get("file_path")
                head = "ide " + (b.get("context_type") or "context") + (f" {fp}" if fp else "")
                c = b.get("content", "").strip()
                parts.append(f"[{head}]" + (f" {c}" if c else ""))
        elif bt == "content_block":
            if not strip_tools:
                c = b.get("content", "").strip()
                parts.append(f"[block: {b.get('block_type')}]" + (f" {c}" if c else ""))
        elif bt == "hook":
            if not strip_tools:
                c = (b.get("content") or "").strip()
                if len(c) > _TOOL_RESULT_CAP:
                    c = c[:_TOOL_RESULT_CAP] + "… (truncated)"
                parts.append(f"[hook: {b.get('name')}]" + (f" {c}" if c else ""))
        elif bt == "attachment":
            if not strip_tools:
                parts.append(f"[attachment: {b.get('attachment_type')}]")
        elif bt == "unknown":
            if not strip_tools:
                c = b.get("content", "").strip()
                parts.append(f"[{b.get('event_type')}]" + (f" {c}" if c else ""))
    content = "\n" + "\n".join(parts) if parts else ""
    return f"[ASSISTANT {ts} event:{step['id']}{focus}]{content}"


def _group_steps_into_turns(steps: list[dict]) -> list[list[dict]]:
    """A turn = one USER step + all following ASSISTANT steps until the next USER."""
    turns: list[list[dict]] = []
    cur: list[dict] = []
    for step in steps:
        if step["role"] == "user" and cur:
            turns.append(cur)
            cur = []
        cur.append(step)
    if cur:
        turns.append(cur)
    return turns


def _accumulate_turns(remaining, limit, max_chars, *, strip_tools, strip_thinking,
                      include_results, user_only, focus_event=None):
    """Accumulate turns into one chunk until the char budget (or turn limit) is hit.
    Returns ``(formatted_steps, turns_consumed, total_chars)`` — ``turns_consumed``
    counts every turn advanced past (incl. compaction placeholders), so
    ``offset + turns_consumed`` is the exact resume point."""
    page: list[str] = []
    consumed = 0
    total_chars = 0
    for turn in remaining:
        user_step = turn[0] if turn and turn[0]["role"] == "user" else None
        if user_step is not None and user_step.get("is_compaction"):
            placeholder = f"[COMPACTION event:{user_step['id']}] (context was compacted here)"
            page.append(placeholder)
            total_chars += len(placeholder)
            consumed += 1
            if consumed >= limit:
                break
            continue
        turn_formatted = [
            _format_step(s, strip_tools=strip_tools, strip_thinking=strip_thinking,
                         include_results=include_results, focus_event=focus_event)
            for s in turn
            if not (user_only and s["role"] != "user")
        ]
        turn_text = "\n\n".join(turn_formatted)
        turn_chars = len(turn_text)
        # Size gate: stop *before* a turn that would blow the budget (unless the page
        # is empty — a single oversized turn can't split, so it's emitted whole).
        if max_chars > 0 and total_chars + turn_chars > max_chars and page:
            break
        consumed += 1
        if turn_formatted:
            page.extend(turn_formatted)
            total_chars += turn_chars
        if consumed >= limit:
            break
    return page, consumed, total_chars


# Rendering caps for a topic read: quotes are the payload so they render whole-ish,
# but a huge topic must not blow the MCP output budget — the footer points at the
# per-citation surface (librarian topic_members) for the full set.
_TOPIC_READ_MAX_CITATIONS = 100
_TOPIC_READ_QUOTE_CHARS = 500


def _topic_read_message(thread: Thread, *, session: Optional[Session] = None) -> str:
    """A topic thread read as its curated page: description, links, and the live
    citations with their quotes — each anchored ``[thread N event:M]`` so it opens
    in ``thread_read`` via ``around_event``."""
    from .._knowledge import read as kg_read

    detail = kg_read.topic_get(thread.id, session=session)
    members = kg_read.topic_members(
        thread.id, limit=_TOPIC_READ_MAX_CITATIONS, session=session)

    lines = [
        f"# Topic {thread.id}: {detail['title'] or '(untitled)'}",
        f"Kind: {detail['topic_kind'] or 'topic'}"
        + (" · archived" if detail["archived"] else ""),
    ]
    if detail["description"]:
        lines += ["", detail["description"]]

    if detail["links"]:
        lines += ["", "## Links"]
        for lk in detail["links"]:
            arrow = "→" if lk["direction"] == "out" else "←"
            lines.append(
                f"- {arrow} {lk['link_type']} [{lk['other_type']} {lk['other_id']}] "
                f"{lk['other_title'] or '(untitled)'}"
                + (f" — {lk['evidence']}" if lk["evidence"] else "")
            )

    if members:
        lines += ["", f"## Citations ({detail['citation_count']})"]
        by_thread: dict[int, list[dict]] = {}
        for m in members:
            by_thread.setdefault(m["thread_id"], []).append(m)
        for tid, cites in by_thread.items():
            lines += ["", f"### Thread {tid}: {cites[0]['thread_title'] or '(untitled)'}"]
            for c in cites:
                quote = (c["quote"] or "").strip()
                if len(quote) > _TOPIC_READ_QUOTE_CHARS:
                    quote = quote[:_TOPIC_READ_QUOTE_CHARS] + "…"
                lines.append(f"- [event:{c['event_id']}] {quote}")
        if detail["citation_count"] > len(members):
            lines += ["", f"(showing {len(members)} of {detail['citation_count']} "
                          f"citations — the librarian MCP's topic_members lists them all)"]
    else:
        lines += ["", "No live citations yet."]

    if detail["peers"]:
        peers = ", ".join(
            f"{p.get('title') or p['thread_id']}" for p in detail["peers"])
        lines += ["", f"Community peers: {peers}"]

    lines += ["", "This is a topic thread — it collects references to messages in "
                  "conversation threads. Open a citation with "
                  "thread_read(thread_id, around_event=<event id>)."]
    return "\n".join(lines)


# Feature flag for the stored-summary read kinds (summary='short'/'indexed').
# On by default; set THREAD_ARCHIVE_STORED_SUMMARIES=0 (or false/no/off) to disable —
# those kinds then return a disabled notice, and everything else is unchanged.
_ENV_STORED_SUMMARIES = "THREAD_ARCHIVE_STORED_SUMMARIES"


def _stored_summaries_enabled() -> bool:
    """Checked per call, so flipping the env var needs no restart for in-process
    callers (a long-lived MCP server picks it up on its next environment)."""
    v = os.environ.get(_ENV_STORED_SUMMARIES, "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _resolve_summary_kind(summary: bool | str) -> Optional[str]:
    """Map the ``summary`` knob → ``None`` (normal read), ``'toc'``, ``'short'``,
    ``'indexed'``, or ``'?'`` for an unrecognised string (the caller reports it).
    Bool-ish strings are accepted because MCP clients sometimes stringify booleans."""
    if isinstance(summary, str):
        v = summary.strip().lower()
        if v in ("short", "indexed", "toc"):
            return v
        if v in ("true", "1", "yes"):
            return "toc"
        if v in ("", "false", "0", "no", "none"):
            return None
        return "?"
    return "toc" if summary else None


def _stored_summary(thread: Thread, kind: str) -> str:
    """The thread's stored summary: ``short`` (``Thread.summary``, a few sentences)
    or ``indexed`` (``Thread.indexed_summary``, structured markdown with event
    anchors). Written by the ``/librarian`` skill via the librarian MCP's
    ``thread_set_summary``, so not every thread has them; absence names whichever
    alternative exists rather than returning empty."""
    text = thread.summary if kind == "short" else thread.indexed_summary
    other_kind = "indexed" if kind == "short" else "short"
    other_text = thread.indexed_summary if kind == "short" else thread.summary
    if not (text and text.strip()):
        hint = (
            f" The {other_kind} summary exists: summary='{other_kind}'."
            if other_text and other_text.strip()
            else " Neither stored summary exists; summary=true gives the message TOC."
        )
        return f"Thread {thread.id} has no {kind} summary.{hint}"
    return (
        f"# Thread {thread.id}: {thread.title or thread.name or '(untitled)'} "
        f"({kind} summary)\n\n{text.strip()}"
    )


def _thread_read_summary(
    thread: Thread, steps: list[dict], limit: int, offset: int, *, session: Optional[Session] = None
) -> str:
    """Compact TOC — one row per message (user step / assistant step), with preview."""
    rows = []
    for st in steps:
        if st["role"] == "user":
            rows.append((st["id"], "user", st["ts"], st["content"]))
        else:
            preview = next((b["content"] for b in st.get("blocks", []) if b.get("type") == "text"), None)
            rows.append((st["id"], "asst", st["ts"], preview or "[tool use]"))
    total = len(rows)
    if total == 0:
        if thread.thread_type == "topic":
            return _topic_read_message(thread, session=session)
        return f"Thread {thread.id} has no messages yet"
    if offset < 0:
        offset = max(0, total + offset)
    if offset >= total:
        return f"Thread {thread.id}: offset {offset} is past the end ({total} messages total)"
    page = rows[offset:offset + limit]
    lines = [
        f"# Thread {thread.id}: {thread.title or thread.name or '(untitled)'} ({total} messages)",
        f"Created: {_fmt_ts(thread.inserted_at)}",
        f"Showing {offset + 1}-{offset + len(page)} of {total}",
        "",
        "| # | Role | Event ID | Time | Preview |",
        "|---|------|----------|------|---------|",
    ]
    for i, (eid, role, ts, preview) in enumerate(page):
        num = offset + i + 1
        pv = (preview or "").replace("|", "/").replace("\n", " ").strip()
        if len(pv) > 70:
            pv = pv[:67] + "..."
        lines.append(f"| {num} | {role} | {eid} | {_fmt_hm(ts)} | {pv} |")
    return "\n".join(lines)


def read_thread(
    thread_id: int | str,
    *,
    limit: int = 200,
    offset: int = 0,
    summary: bool | str = False,
    mode: Optional[str] = None,
    user_only: Optional[bool] = None,
    tool_results: bool = False,
    max_chars: int = 0,
    after_event: Optional[int] = None,
    around_event: Optional[int] = None,
    context_turns: int = 1,
    session: Optional[Session] = None,
) -> str:
    """Read a thread's conversation as a transcript, reconstructed from its events.

    ``thread_id`` is either the archive's integer thread id or a provider **session
    id** (the uuid/source_id a tool knows the conversation by) — see
    :func:`resolve_thread_ref`. ``mode`` picks the view: ``user`` (default) = only
    the user turns; ``chat`` = user + assistant visible text (thinking + tool calls
    stripped); ``full`` = the whole transcript including tool calls. ``tool_results``
    (default off) adds tool *output* under each call — only meaningful in ``full``
    (where calls are shown). The read is paginated by turns and size-budgeted at
    ``max_chars`` (default ~48k chars): a thread bigger than one chunk ends in a
    CHUNKED footer naming the next offset. ``after_event`` resumes from the turn after
    an event id. ``around_event`` opens a search result in its containing turn plus
    ``context_turns`` turns on each side; it overrides offset/after-event pagination
    and defaults to the readable ``chat`` view when no mode is explicit. A hit on an
    event the transcript hides opens the turn at its position (with no ``match:``
    marker, since the event itself isn't rendered). ``summary``
    picks a summary view instead of the transcript —
    ``True``/``'toc'`` = compact per-message TOC, ``'short'`` = the stored short
    summary (``Thread.summary``), ``'indexed'`` = the stored indexed summary
    (``Thread.indexed_summary``, structured, with event anchors). ``user_only`` is a
    back-compat alias for ``mode`` (True→user, False→full); ``mode`` wins. Returns a
    message string if absent.
    """
    summary_kind = _resolve_summary_kind(summary)
    if summary_kind == "?":
        return (
            f"Unknown summary kind {summary!r} — use 'short' (stored short summary), "
            f"'indexed' (stored indexed summary), or true/'toc' (compact message TOC)."
        )
    if summary_kind in ("short", "indexed") and not _stored_summaries_enabled():
        return (
            f"Stored-summary reads are disabled ({_ENV_STORED_SUMMARIES} is off). "
            f"summary=true still gives the compact message TOC."
        )

    with use_session(session) as s:
        resolved = resolve_thread_ref(s, thread_id)
        if resolved is None:
            return f"Thread {thread_id} not found."
        thread_id = resolved
        thread = s.get(Thread, thread_id)
        if thread is None:
            return f"Thread {thread_id} not found."
        if summary_kind in ("short", "indexed"):
            return _stored_summary(thread, summary_kind)
        events = s.execute(
            select(Event).where(Event.thread_id == thread_id).order_by(Event.id)
        ).scalars().all()

    steps = _build_steps(_absorb_stream_deltas(_slot_queued_events(events)))

    if summary_kind == "toc":
        return _thread_read_summary(
            thread, steps, limit if limit and limit > 0 else 200, offset, session=session
        )

    # A focused search-result read should show the exchange around the hit, not only
    # the asking side. An explicit mode/user_only remains authoritative.
    effective_mode = (
        "chat" if around_event is not None and mode is None and user_only is None else mode
    )
    resolved_user_only, strip_tools, strip_thinking = resolve_read_view(effective_mode, user_only)
    budget = max_chars if max_chars and max_chars > 0 else DEFAULT_READ_CHAR_BUDGET

    turns = _group_steps_into_turns(steps)

    focus_turn: Optional[int] = None
    if around_event is not None:
        for i, turn in enumerate(turns):
            if any(around_event in st.get("event_ids", [st["id"]]) for st in turn):
                focus_turn = i
                break
        if focus_turn is None:
            # The event may exist but be hidden from the transcript (lifecycle noise,
            # codex machinery, text deduped against an already-rendered turn) — search
            # indexes some of those, so a real hit id can be absent from every step.
            # Fall back to its position: the last turn with a rendered event before it.
            if any(ev.id == around_event for ev in events):
                focus_turn = 0
                for i, turn in enumerate(turns):
                    if any(
                        (eid or 0) <= around_event
                        for st in turn for eid in st.get("event_ids", [st["id"]])
                    ):
                        focus_turn = i
            else:
                return f"Event {around_event} was not found in thread {thread_id}."
        if context_turns < 0:
            return "context_turns must be zero or greater."
        offset = max(0, focus_turn - context_turns)

    # after_event → offset: resume from the turn AFTER the last turn at/with that id.
    if around_event is None and after_event is not None:
        last_before = -1
        for i, turn in enumerate(turns):
            if any((st["id"] or 0) <= after_event for st in turn):
                last_before = i
        offset = last_before + 1

    total = len(turns)
    if offset < 0:
        offset = max(0, total + offset)
    requested_end = (
        min(total, focus_turn + context_turns + 1) if focus_turn is not None else total
    )
    remaining = turns[offset:requested_end]
    page_limit = len(remaining) if focus_turn is not None else limit

    page, consumed, total_chars = _accumulate_turns(
        remaining, page_limit, budget,
        strip_tools=strip_tools, strip_thinking=strip_thinking,
        include_results=tool_results, user_only=resolved_user_only,
        focus_event=around_event,
    )

    # A huge preceding context turn must never consume the budget before the match.
    # If that happened, drop only the preceding context and retry from the focus.
    if focus_turn is not None and offset + consumed <= focus_turn:
        offset = focus_turn
        remaining = turns[offset:requested_end]
        page, consumed, total_chars = _accumulate_turns(
            remaining, len(remaining), budget,
            strip_tools=strip_tools, strip_thinking=strip_thinking,
            include_results=tool_results, user_only=resolved_user_only,
            focus_event=around_event,
        )

    if not page:
        if offset > 0:
            return f"Thread {thread_id}: offset {offset} is past the end ({total} turns total)"
        if thread.thread_type == "topic":
            return _topic_read_message(thread, session=session)
        return f"Thread {thread_id} has no messages yet"

    next_offset = offset + consumed
    label = "user turns" if resolved_user_only else "turns"
    focus_note = (
        f", focused on event {around_event} in turn {focus_turn + 1}"
        if focus_turn is not None else ""
    )
    header = (
        f"# Thread {thread_id}: {thread.title or thread.name or '(untitled)'}\n"
        f"Created: {_fmt_ts(thread.inserted_at)}\n"
        f"Messages: {total} {label}{focus_note}, showing {offset + 1}-{next_offset} "
        f"(~{total_chars} chars)\n\n"
    )
    body = "\n\n".join(page)

    footer = ""
    if next_offset < requested_end:
        remaining_turns = requested_end - next_offset
        cont = f"thread_id={thread_id}, offset={next_offset}"
        if not resolved_user_only:
            cont += ", user_only=false"
        why = f"~{budget}-char budget" if budget > 0 else f"{limit}-turn limit"
        footer = (
            f"\n\n---\n"
            f"⚠ CHUNKED at the {why} — {remaining_turns} of {total} {label} not shown "
            f"(this is NOT the whole thread). "
            f"Read the next chunk: thread_read({cont})"
        )
    return header + body + footer


# Cap a single tool result so a megabyte of command output never ships to the
# viewer whole; the UI shows the head and flags the truncation.
_TOOL_OUTPUT_CAP = 20_000

# Placeholder model strings that aren't a real model id (mirrors the importer's
# NON_MODEL_VALUES) — kept out of a message's model list.
_NON_MODEL_VALUES = ("", "unknown", "<synthetic>")

# The api_request lifecycle events (normally hidden by _SKIP_TYPES) that carry a
# message's model/token/stop-reason — folded into per-message meta, not rendered.
_REQUEST_TYPES = frozenset({"api_request_started", "api_request_completed"})


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if hasattr(dt, "isoformat") else (str(dt) if dt else None)


def _new_meta(role: str, ev: Event) -> dict:
    """A fresh per-message meta record. Assistant messages carry the model/request
    fields the info drawer fills from api_request events; a user (or other-role)
    message just carries its timestamp."""
    meta: dict = {"ts": _iso(ev.occurred_at)}
    if role == "assistant":
        meta.update(models=[], requests=0, stop_reason=None,
                    tokens={"input": 0, "output": 0, "thinking": 0})
    return meta


def _fold_request(meta: dict, event_type: str, p: dict) -> None:
    """Fold one api_request_{started,completed} payload into a message's meta: the
    model (deduped), and — from the *completed* event — token counts, a request
    tally, and the stop reason. This is what lets the drawer show that a single
    turn's tool loop ran N requests, and which model(s) answered it."""
    model = p.get("model")
    if model and model not in _NON_MODEL_VALUES and model not in meta["models"]:
        meta["models"].append(model)
    if event_type == "api_request_completed":
        meta["requests"] += 1
        meta["tokens"]["input"] += int(p.get("input_tokens") or 0)
        meta["tokens"]["output"] += int(p.get("output_tokens") or 0)
        meta["tokens"]["thinking"] += int(p.get("thinking_tokens") or 0)
        stop = p.get("stop_reason")
        if stop:
            meta["stop_reason"] = stop


def _structured_event(
    ev: Event, *, include_thinking: bool, include_tools: bool, rendered_text: Container[str]
) -> Optional[tuple[str, dict]]:
    """Like the string transcript renderer, but returns ``(role, block)`` where ``block`` is a
    typed renderable dict — so the web viewer can render markdown, syntax-highlighted
    code, and collapsible tool calls instead of a pre-flattened string. Mirrors the
    same event-type handling; returns None to skip lifecycle/empty events."""
    et = ev.event_type
    # Hook visibility — handled ahead of the _SKIP_TYPES gate that hides these on
    # the string path. A hook that fired is part of the who-did-what record: shown
    # here, in the operator-facing viewer.
    if et == "hook_context":
        # A hook-context sidecar line: content a hook injected that never reaches
        # the session JSONL (e.g. cloth's per-prompt kg_context).
        p = _payload(ev)
        text = p.get("context", "")
        if not text.strip():
            return None
        return ("assistant", {"type": "hook", "hook_name": p.get("hook_name") or "hook",
                              "text": text})
    if et == "progress":
        if not include_tools:
            return None
        p = _payload(ev)
        data = p.get("data") or {}
        if not isinstance(data, dict) or data.get("type") != "hook_progress":
            return None  # other progress kinds stay bookkeeping noise
        # A bare "this hook ran" marker (no content) — the viewer merges runs of
        # these into one compact row next to the tool calls they fired on.
        name = data.get("hookName") or data.get("hookEvent")
        return ("assistant", {"type": "hook_fired", "hook_name": name}) if name else None
    if et in _SKIP_TYPES:
        return None
    p = _payload(ev)

    if et in ("user_message_sent", "thread_message_sent"):
        content = p.get("content", "")
        if not content.strip():
            return None
        return ("user", {"type": "text", "text": _display_user_content(content)})
    if et == "text_complete":
        text = p.get("text", "")
        return ("assistant", {"type": "text", "text": text}) if text.strip() else None
    if et == "thinking_complete":
        if not include_thinking:
            return None
        text = p.get("text", "")
        return ("assistant", {"type": "thinking", "text": text}) if text.strip() else None
    if et in ("tool_use_complete", "tool_use_started"):
        if not include_tools:
            return None
        return ("assistant", {
            "type": "tool_use",
            "name": p.get("tool_name", "unknown"),
            "input": p.get("input", {}),
        })
    if et == "tool_execution_completed":
        if not include_tools:
            return None
        out = str(p.get("output", ""))
        return ("assistant", {
            "type": "tool_result",
            "output": out[:_TOOL_OUTPUT_CAP],
            "truncated": len(out) > _TOOL_OUTPUT_CAP,
        })
    if et == "tool_execution_error":
        if not include_tools:
            return None
        return ("assistant", {"type": "tool_error", "error": str(p.get("error", ""))})
    if et == "context_summary":
        att = _attachment_raw(p)
        if att is not None:
            atype = att.get("type") or "attachment"
            if atype == "hook_additional_context":
                # A hook fired and injected content into the model's context (e.g.
                # a UserPromptSubmit hook adding system-map notes). First-class:
                # the hook's name plus exactly what it injected — conversation
                # context, so not gated behind the tools toggle.
                return ("assistant", {"type": "hook", "hook_name": _hook_name(att),
                                      "text": _attachment_text(att)})
            # Every other preserved attachment (todo reminders, skill/tool listing
            # deltas, …) is machinery: behind the tools toggle, but carrying its
            # real content, not just the "[attachment: …]" placeholder.
            if not include_tools:
                return None
            return ("assistant", {"type": "attachment", "attachment_type": atype,
                                  "text": _attachment_text(att)})
        content = p.get("content", "")
        if not content.strip():
            return None
        # Claude Code records a model_refusal_fallback (a message the active model's
        # safeguards flagged, retried on a stronger model) as a context_summary. Surface
        # it as a distinct, legible safeguard notice rather than a generic "context
        # summary" — it explains an otherwise-mysterious mid-turn model switch.
        low = content.lower()
        if "safeguard" in low and "flag" in low:
            return ("assistant", {"type": "safeguard_notice", "text": content})
        return ("assistant", {"type": "context_summary", "text": content})
    if et == "model_change":
        # A manual `/model` switch — a standalone divider between turns (own role, so it
        # renders bare, not inside a bubble). Genuine context, not gated behind tools.
        to = p.get("to")
        if not to:
            return None
        return ("model_switch", {"type": "model_switch", "kind": "user",
                                 "from_model": None, "to_model": to})
    if et == "message":
        # A preserved non-standard-role turn — shown under its own role (genuine
        # content, not machinery, so not gated behind include_tools).
        content = p.get("content", "")
        return (p.get("role") or "message", {"type": "text", "text": content}) if content.strip() else None
    if et == "ide_context":
        if not include_tools:
            return None
        return ("assistant", {
            "type": "ide_context",
            "context_type": p.get("context_type") or "context",
            "file_path": p.get("file_path"),
            "text": p.get("content", ""),
        })
    if et == "content_block":
        data = p.get("data")
        # A model-fallback marker (Claude Code retried a safeguard-flagged message on a
        # stronger model) carries the from/to models — surface it as a first-class model
        # switch marker, not a cryptic "block · fallback". Genuine conversation context,
        # so shown regardless of the tools toggle.
        if p.get("block_type") == "fallback":
            raw = data.get("raw") if isinstance(data, dict) else None
            frm = to = None
            if isinstance(raw, dict):
                frm = (raw.get("from") or {}).get("model")
                to = (raw.get("to") or {}).get("model")
            return ("assistant", {"type": "model_switch", "kind": "fallback",
                                  "from_model": frm, "to_model": to})
        if not include_tools:
            return None
        view = _content_block_view(p, rendered_text)
        if view is None:
            return None
        block_type, text = view
        return ("assistant", {"type": "content_block", "block_type": block_type, "text": text})
    # An unrecognized, non-lifecycle type: surface it in the machinery view rather
    # than silently dropping it (a new importer-preserved type stays visible).
    if not include_tools:
        return None
    return ("assistant", {"type": "unknown", "event_type": et, "text": _unknown_payload_text(p)})


def read_thread_structured(
    thread_id: int | str,
    *,
    include_thinking: bool = True,
    include_tools: bool = True,
    session: Optional[Session] = None,
) -> dict:
    """Reconstruct a thread as structured messages for the web viewer.

    ``thread_id`` accepts an integer thread id or a provider session id, same as
    :func:`read_thread` (see :func:`resolve_thread_ref`). Returns
    ``{thread_id, title, source, messages}`` where ``messages`` is a list of
    ``{role, blocks, event_ids, meta}`` — same-role events grouped into a message
    (``event_ids`` names the source events, so the viewer can deep-link a search hit
    to its message), but an assistant
    turn's tool loop is split at each api_request boundary so every model inference (one
    tool-call/response iteration) is its own message. Each block is a typed dict
    (:func:`_structured_event`); ``meta`` is per-message info (timestamp, and for
    assistant messages the model/token/stop-reason folded from that inference's
    api_request events) for the viewer's info drawer and per-message model tint. A model
    switch mid-turn thus lands on a message boundary instead of hiding inside one merged
    bubble. Providers without api_request events keep one message per turn (nothing to
    split on). The string :func:`read_thread` stays the canonical transcript (CLI /
    MCP); this is the render-friendly sibling."""
    with use_session(session) as s:
        resolved = resolve_thread_ref(s, thread_id)
        if resolved is None:
            return {"thread_id": thread_id, "title": None, "source": None, "messages": []}
        thread = s.get(Thread, resolved)
        if thread is None:
            return {"thread_id": thread_id, "title": None, "source": None, "messages": []}
        events = s.execute(
            select(Event).where(Event.thread_id == resolved).order_by(Event.id)
        ).scalars().all()
    events = _absorb_stream_deltas(_slot_queued_events(events))
    rendered_text = _rendered_text(events)

    messages: list[dict] = []
    current: Optional[dict] = None
    # api_request events for an inference precede its first visible block; buffer them
    # until the assistant message they belong to exists (and drop the buffer at a role
    # change so a fully-hidden inference's request can't leak onto the next message).
    pending: list[tuple[str, dict]] = []
    # A single assistant turn is a tool loop of N model inferences, each opened by an
    # api_request_started. We split the turn into one message per inference — a
    # tool-call/response iteration — rather than collapse the whole turn into one bubble.
    # That keeps every message's model exact, so a mid-turn model switch surfaces as a
    # fresh (differently-tinted) message instead of hiding inside one merged turn.
    split = False  # an api_request_started opened a new inference; next block starts a message
    for ev in events:
        if ev.event_type in _REQUEST_TYPES:
            p = _payload(ev)
            # A new inference inside the current assistant turn → force the next visible
            # block to start a fresh message, and buffer this request so its model/tokens
            # fold into that new message rather than the inference that just ended.
            if (
                ev.event_type == "api_request_started"
                and current is not None
                and current["role"] == "assistant"
                and current["blocks"]
            ):
                split = True
            if not split and current is not None and current["role"] == "assistant":
                _fold_request(current["meta"], ev.event_type, p)
            else:
                pending.append((ev.event_type, p))
            continue
        rendered = _structured_event(
            ev, include_thinking=include_thinking, include_tools=include_tools,
            rendered_text=rendered_text,
        )
        if rendered is None:
            continue
        role, block = rendered
        if split or current is None or current["role"] != role:
            current = {"role": role, "blocks": [], "event_ids": [], "meta": _new_meta(role, ev)}
            if role == "assistant":
                for pet, pp in pending:
                    _fold_request(current["meta"], pet, pp)
            pending = []
            split = False
            messages.append(current)
        current["blocks"].append(block)
        # The events this message's blocks came from — lets the viewer resolve a
        # search hit's event id to its message (deep-link + highlight).
        if ev.id is not None and ev.id not in current["event_ids"]:
            current["event_ids"].append(ev.id)

    return {
        "thread_id": thread.id,
        "title": thread.title or thread.name,
        "source": thread.source,
        "messages": messages,
    }
