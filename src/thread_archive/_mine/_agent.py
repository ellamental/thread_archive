"""The headless-agent seam — one ``claude`` turn-loop, shared by every miner.

Neither this module nor its caller interprets the reply: a miner asks the agent
for a particular JSON shape and parses whatever comes back, so the same runner
drives the query miner's verdict, the topic survey/labeler, the rerank judge, and
the query generator. Bash is allowed only for the snapshot-bound corpus seam
(``thread_archive._mine tool ...``), so a mining agent can search and read the
frozen corpus and nothing else.
"""

from __future__ import annotations

import json
import subprocess

# The claude CLI's alias for the latest Opus — mining is judgment work, the
# capable tier is the point.
DEFAULT_MODEL = "opus"

# One agent run explores with many tool calls; cap the loop and the wall clock so
# a wedged agent costs a bounded amount and is scored as a failure. The deep
# corpus-sweep miners (query, topic) want the full budget; the single-pass judge
# (rerank) overrides to a smaller one.
AGENT_MAX_TURNS = 60
AGENT_TIMEOUT_S = 1500


def run_claude(prompt: str, model: str, tool_cmd: str, *,
               max_turns: int = AGENT_MAX_TURNS,
               timeout: int = AGENT_TIMEOUT_S,
               runner=subprocess.run) -> tuple[str | None, dict]:
    """One headless agent turn-loop. Returns (final message text | None, stats).
    Bash is allowed only for ``tool_cmd`` (the snapshot-bound corpus access); any
    failure — nonzero exit, timeout, unparseable envelope — is (None, stats) with
    an ``error`` note, so the caller scores it as a failed case rather than
    crashing the run. ``runner`` is the subprocess seam (default the real
    ``subprocess.run``) — a test supplies a fake to exercise the envelope handling
    without shelling ``claude``."""
    stats: dict = {}
    try:
        proc = runner(
            ["claude", "-p", prompt, "--output-format", "json",
             "--model", model, "--max-turns", str(max_turns),
             "--allowedTools", f"Bash({tool_cmd}:*)"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            stats["error"] = (proc.stderr or "").strip()[-500:]
            return None, stats
        out = json.loads(proc.stdout)
        stats = {"num_turns": out.get("num_turns"),
                 "cost_usd": out.get("total_cost_usd"),
                 "duration_ms": out.get("duration_ms")}
        return (out.get("result") or ""), stats
    except subprocess.TimeoutExpired:
        stats["error"] = "timeout"
        return None, stats
    except (json.JSONDecodeError, OSError) as e:
        stats["error"] = str(e)[:200]
        return None, stats
