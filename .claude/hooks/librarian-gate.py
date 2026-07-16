#!/usr/bin/env python3
"""librarian gate — enforces strict one-thread-at-a-time processing.

When `/librarian` is invoked (interactively or by the bulk backfill driver), this hook
locks the session into a per-thread state machine that makes the skill's loop
mechanically impossible to skip:

  read ONE thread (thread_user_messages / thread_read)
    -> commit it (>=1 topic citation AND a stored summary — the two halves of the
       per-thread commit)
      -> only THEN read the next thread.

The failure this prevents: the model batch-reading the whole queue first ("I've read
all 25 threads…") before doing any writing — which bloats context, loses per-thread
focus, and loses everything on a mid-run death — or citing every thread while skipping
the summaries. Without this gate, an instance reliably under-does the work; with it,
every thread is finished before the next is opened.

Enforcement:
  - Opening a *different* thread before the current one has both a citation and a
    summary: BLOCKED.
  - Stop while a thread is open missing either half: BLOCKED (3-strike safety release).

Everything else (review_queue, topic_search, topic_create/link, the commit calls, …)
passes straight through. Sessions that never ran `/librarian` are completely unaffected —
every mode fast-returns when state isn't active.

Modes (argv[1]):
  reset  (UserPromptSubmit): detect /librarian, arm fresh state
  pre    (PreToolUse):       gate tool calls
  post   (PostToolUse):      advance state after a call runs
  stop   (Stop):             block end-of-turn while a thread is open uncommitted

Fail-open by design: any error allows the call. A hook bug must never wedge a session.
State dir is `$THREAD_LIBRARIAN_GATE_DIR` (else /tmp/thread-archive-librarian-gate), so a
test (or a sandbox) can isolate it.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("THREAD_LIBRARIAN_GATE_DIR", "/tmp/thread-archive-librarian-gate"))

# Reading a conversation: either the cheap user-messages read (librarian MCP) or the
# full transcript (read-only thread-archive MCP). Both carry `thread_id`.
THREAD_READ_TOOLS = {
    "mcp__thread-archive-librarian__thread_user_messages",
    "mcp__thread-archive__thread_read",
}
# The two halves of the per-thread commit. Both carry the conversation's `thread_id`.
CITE_TOOL = "mcp__thread-archive-librarian__topic_cite"
SUMMARY_TOOL = "mcp__thread-archive-librarian__thread_set_summary"

# Consecutive Stop blocks tolerated before releasing (so a persistently-failing run
# can't infinite-loop burning tokens).
MAX_STOP_BLOCKS = 3

# Never gated — session housekeeping that must always work.
ALWAYS_ALLOWED = {"ToolSearch", "TodoWrite", "EnterPlanMode", "ExitPlanMode", "Skill"}

mode = sys.argv[1] if len(sys.argv) > 1 else "pre"

try:
    input_data = json.load(sys.stdin)
except Exception:
    input_data = {}


def _session_key():
    sid = input_data.get("session_id")
    basis = str(sid) if sid else os.getcwd()
    return hashlib.md5(basis.encode()).hexdigest()[:12]


def _state_path():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{_session_key()}.json"


def _load():
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return None


def _save(state):
    _state_path().write_text(json.dumps(state, indent=2))


def _clear():
    try:
        _state_path().unlink()
    except Exception:
        pass


def _block(reason):
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.exit(0)


def _thread_id(tool_input):
    tid = tool_input.get("thread_id")
    try:
        return int(tid) if tid is not None else None
    except (TypeError, ValueError):
        return None


def _committed(state):
    return state.get("citations", 0) >= 1 and state.get("summaries", 0) >= 1


def _missing(state, tid):
    parts = []
    if state.get("citations", 0) < 1:
        parts.append(f">=1 citation (topic_cite(thread_id={tid}, ...))")
    if state.get("summaries", 0) < 1:
        parts.append(f"a stored summary (thread_set_summary(thread_id={tid}, ...))")
    return " and ".join(parts)


def reset():
    message = (input_data.get("message") or input_data.get("prompt") or "").strip()

    # Any new user prompt clears stale state (escape valve).
    _clear()

    if not message.startswith("/librarian"):
        return

    _save({
        "active": True,
        "current_thread": None,
        "citations": 0,
        "summaries": 0,
        "stop_blocks": 0,
    })

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "ONE-THREAD-AT-A-TIME ENFORCEMENT ACTIVE (librarian-gate).\n"
                "Process the queue strictly one thread at a time, finishing each before "
                "touching the next:\n"
                "  1. thread_user_messages(thread_id=X) — read ONE thread.\n"
                "  2. Cite it: topic_cite(...) at least once.\n"
                "  3. Summarize it: thread_set_summary(...) — 2+3 together ARE the commit.\n"
                "  4. Only THEN read the next thread.\n"
                "Reading a different thread before the current one has BOTH a citation "
                "and a stored summary is BLOCKED. Stop is BLOCKED while a thread is "
                "open missing either. Re-reading/paging the SAME thread, review_queue, "
                "topic_search, topic_create/link are all fine."
            ),
        }
    }
    print(json.dumps(output))


def _pre_thread_read(tool_input, state, current):
    """Block opening a *different* thread while the current one is uncommitted."""
    tid = _thread_id(tool_input)
    if tid is None or tid == current:
        return  # malformed (fail open) or paging the current thread
    if current is None or _committed(state):
        return  # opening the first / next thread — post records the switch
    _block(
        f"librarian-gate: still on thread {current}. Finish it first — add "
        f"{_missing(state, current)} — before reading thread {tid}. "
        f"One thread at a time."
    )


def pre():
    state = _load()
    if not state or not state.get("active"):
        return

    tool_name = input_data.get("tool_name", "")
    if tool_name in ALWAYS_ALLOWED:
        return

    tool_input = input_data.get("tool_input", {}) or {}
    current = state.get("current_thread")

    if tool_name in THREAD_READ_TOOLS:
        _pre_thread_read(tool_input, state, current)


def post():
    state = _load()
    if not state or not state.get("active"):
        return

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {}) or {}
    current = state.get("current_thread")

    # A read switched to a new thread — open it fresh.
    if tool_name in THREAD_READ_TOOLS:
        tid = _thread_id(tool_input)
        if tid is not None and tid != current:
            state.update(current_thread=tid, citations=0, summaries=0, stop_blocks=0)
            _save(state)
        return

    # A commit half landed for the open conversation.
    if tool_name in (CITE_TOOL, SUMMARY_TOOL) and current is not None:
        if _thread_id(tool_input) in (current, None):
            key = "citations" if tool_name == CITE_TOOL else "summaries"
            state[key] = state.get(key, 0) + 1
            state["stop_blocks"] = 0
            _save(state)
        return


def stop():
    state = _load()
    if not state or not state.get("active"):
        return

    current = state.get("current_thread")
    # Nothing open (queue may simply be clear), or current thread committed — fine.
    if current is None or _committed(state):
        return

    blocks = state.get("stop_blocks", 0)
    if blocks >= MAX_STOP_BLOCKS:
        _clear()  # safety release — don't trap a failing run forever
        return

    state["stop_blocks"] = blocks + 1
    _save(state)
    _block(
        f"librarian-gate: thread {current} is open but uncommitted. Finish it — add "
        f"{_missing(state, current)} — before stopping. Do NOT stop mid-thread."
    )


try:
    {"reset": reset, "pre": pre, "post": post, "stop": stop}[mode]()
except Exception:
    pass  # fail open — never wedge a session on a hook bug
