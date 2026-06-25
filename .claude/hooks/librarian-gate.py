#!/usr/bin/env python3
"""librarian gate — enforces strict one-thread-at-a-time processing.

When `/librarian` is invoked (interactively or by the bulk backfill driver), this hook
locks the session into a per-thread state machine that makes the skill's loop
mechanically impossible to skip:

  read ONE thread (thread_user_messages / thread_read)
    -> link it (>=1 topic citation)
      -> commit it (thread_set_summary)
        -> only THEN read the next thread.

The failure this prevents: the model batch-reading the whole queue first ("I've read
all 25 threads…") before doing any linking or summarizing — which bloats context, loses
per-thread focus, and loses everything on a mid-run death. Without this gate, an instance
reliably under-does the work; with it, every thread is finished before the next is opened.

Enforcement:
  - Opening a *different* thread before the current one is committed: BLOCKED.
  - Committing a thread's summary with zero citations: BLOCKED.
  - Stop while a thread is open-but-uncommitted: BLOCKED (3-strike safety release).

Everything else (review_queue, topic_search, topic_create/link, the citation calls, …)
passes straight through. Sessions that never ran `/librarian` are completely unaffected —
every mode fast-returns when state isn't active.

Modes (argv[1]):
  reset  (UserPromptSubmit): detect /librarian, arm fresh state
  pre    (PreToolUse):       gate tool calls
  post   (PostToolUse):      advance state after a call runs
  stop   (Stop):             block end-of-turn while a thread is uncommitted

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
# A citation for the open conversation (its `thread_id` is the conversation thread).
CITE_TOOL = "mcp__thread-archive-librarian__topic_cite"
# The per-thread commit point.
SUMMARY_TOOL = "mcp__thread-archive-librarian__thread_set_summary"

# Minimum citations required before a thread's summary can be committed.
MIN_CITATIONS = 1
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


def reset():
    message = (input_data.get("message") or input_data.get("prompt") or "").strip()

    # Any new user prompt clears stale state (escape valve).
    _clear()

    if not message.startswith("/librarian"):
        return

    _save({
        "active": True,
        "current_thread": None,
        "committed": False,
        "citations": 0,
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
                "  2. Link it: topic_cite(...) at least once.\n"
                "  3. Commit it: thread_set_summary(thread_id=X, ...).\n"
                "  4. Only THEN read the next thread.\n"
                "Reading a different thread before the current one is committed is BLOCKED. "
                "Committing a summary with zero citations is BLOCKED. Stop is BLOCKED while a "
                "thread is open but uncommitted. Re-reading/paging the SAME thread, "
                "review_queue, topic_search, topic_create/link are all fine."
            ),
        }
    }
    print(json.dumps(output))


def _pre_thread_read(tool_input, current, committed):
    """Block opening a *different* thread while the current one is uncommitted."""
    tid = _thread_id(tool_input)
    if tid is None or tid == current:
        return  # malformed (fail open) or paging the current thread
    if current is None or committed:
        return  # opening the first / next thread — post records the switch
    _block(
        f"librarian-gate: still on thread {current}. Finish it first — add >=1 citation "
        f"(topic_cite) and commit its summary (thread_set_summary(thread_id={current})) — "
        f"before reading thread {tid}. One thread at a time."
    )


def _pre_summary_commit(state, tool_input, current):
    """Block a thread_set_summary with no/wrong open thread or too few citations."""
    tid = _thread_id(tool_input)
    if current is None:
        _block("librarian-gate: no thread is open. Read a thread (thread_user_messages) "
               "before writing a summary.")
    if tid is not None and tid != current:
        _block(f"librarian-gate: write the summary for the thread you're working ({current}), "
               f"not {tid}. Finish {current} first.")
    if state.get("citations", 0) < MIN_CITATIONS:
        _block(f"librarian-gate: link thread {current} before committing it — "
               f"{state.get('citations', 0)} citations so far, need >={MIN_CITATIONS}. "
               f"Add a topic_cite first.")


def pre():
    state = _load()
    if not state or not state.get("active"):
        return

    tool_name = input_data.get("tool_name", "")
    if tool_name in ALWAYS_ALLOWED:
        return

    tool_input = input_data.get("tool_input", {}) or {}
    current = state.get("current_thread")
    committed = state.get("committed", False)

    if tool_name in THREAD_READ_TOOLS:
        _pre_thread_read(tool_input, current, committed)

    if tool_name == SUMMARY_TOOL:
        _pre_summary_commit(state, tool_input, current)


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
            state.update(current_thread=tid, committed=False, citations=0, stop_blocks=0)
            _save(state)
        return

    # A citation landed for the open conversation.
    if tool_name == CITE_TOOL and current is not None:
        if _thread_id(tool_input) in (current, None):
            state["citations"] = state.get("citations", 0) + 1
            _save(state)
        return

    # The open thread's summary was committed.
    if tool_name == SUMMARY_TOOL and current is not None:
        tid = _thread_id(tool_input)
        if tid is None or tid == current:
            state.update(committed=True, stop_blocks=0)
            _save(state)
        return


def stop():
    state = _load()
    if not state or not state.get("active"):
        return

    current = state.get("current_thread")
    # Nothing open (queue may simply be clear), or current thread finished — fine.
    if current is None or state.get("committed", False):
        return

    blocks = state.get("stop_blocks", 0)
    if blocks >= MAX_STOP_BLOCKS:
        _clear()  # safety release — don't trap a failing run forever
        return

    state["stop_blocks"] = blocks + 1
    _save(state)
    _block(
        f"librarian-gate: thread {current} is open but not committed "
        f"({state.get('citations', 0)} citations, summary not written). Finish it — add "
        f">=1 citation, then thread_set_summary(thread_id={current}) — before stopping. "
        f"Do NOT stop mid-thread."
    )


try:
    {"reset": reset, "pre": pre, "post": post, "stop": stop}[mode]()
except Exception:
    pass  # fail open — never wedge a session on a hook bug
