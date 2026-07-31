"""Delegating a terminal retrieval call to the shared MCP server.

A CLI one-shot builds a whole serving process for one call — interpreter,
engine import, archive open, and (for a semantic search) the model load — then
exits, so nothing amortizes and every ``thread-archive search`` costs seconds.
The shared HTTP server (``archive-mcp --http``, :8788) holds all of that warm
for every client on the machine. When it is alive and serving the same archive
this process would open, asking it is strictly cheaper than doing the work
here, and the answer is byte-identical: both doors run the same tool.

The offline path stays first-class. Delegation is opportunistic and silent —
any failure (nothing listening, a timeout, a transport error, a tool-level
error) falls back to the in-process engine, which remains the implementation
of record. A tool-level error is deliberately *re-run* locally rather than
relayed: the CLI's error rendering and exit codes are contracts of the
in-process path, and reproducing the failure there keeps them exact, at the
price of a slow path on the rare bad call.

Eligibility is about identity, not liveness: the shared server serves the
machine's default archive, so a call is delegable only when it resolves to
that same home. An explicit ``--home`` or a ``THREAD_ARCHIVE_HOME`` pointing
elsewhere names a different archive and always runs in-process. Liveness is
discovered by just trying — on loopback a dead port refuses instantly.

The wire is one stateless JSON-RPC POST (``tools/call``) to the server's
streamable-http endpoint, which answers plain JSON (``json_response=True`` /
``stateless_http=True`` in :mod:`._mcp.server`) — no session handshake, no
MCP SDK client, no new dependency.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

from ._config import default_home, resolve_paths

#: Where the shared server answers. Override for a nonstandard port; the
#: default matches the ``archive-mcp --http`` LaunchAgent.
ENV_URL = "THREAD_ARCHIVE_MCP_URL"
#: Truthy disables delegation entirely — every call runs in-process.
ENV_OFF = "THREAD_ARCHIVE_NO_DELEGATE"

DEFAULT_URL = "http://127.0.0.1:8788/mcp"

#: Whole-request budget. A dead port refuses in microseconds, so this only
#: binds when the server is up but drowning — past it the call falls back and
#: pays the in-process cost on top, so it is set above the slowest honest
#: query (broad substring scans measure ~9 s) rather than at a snappy ideal.
TIMEOUT_S = 20.0

_TRUTHY = ("1", "true", "yes", "on")


def eligible(home_arg: Optional[str]) -> bool:
    """Whether a call targeting ``home_arg`` may be served by the shared server.

    True exactly when the call resolves to the machine's default archive home —
    the one the shared server serves — and the operator hasn't switched
    delegation off. Spelling the default explicitly (``--home
    ~/.thread/archive``) still qualifies; it is the same archive.
    """
    if os.environ.get(ENV_OFF, "").strip().lower() in _TRUTHY:
        return False
    return resolve_paths(home_arg).home == default_home()


def call(
    tool: str,
    arguments: dict[str, object],
    *,
    url: Optional[str] = None,
    timeout: float = TIMEOUT_S,
) -> Optional[str]:
    """Ask the shared server to run ``tool`` — the rendered text, or None.

    None means "run it yourself": the server is down, slow past the budget,
    answered malformed, or the tool itself errored (relaying an error would
    bypass the CLI's own error contract — the local re-run reproduces it
    through the real path). Never raises.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url or os.environ.get(ENV_URL) or DEFAULT_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = payload["result"]
        if result.get("isError"):
            return None
        parts = [
            c["text"]
            for c in result.get("content", ())
            if c.get("type") == "text" and isinstance(c.get("text"), str)
        ]
        if not parts:
            return None
        return "\n".join(parts)
    except Exception:  # noqa: BLE001 — fallback is the contract
        return None
