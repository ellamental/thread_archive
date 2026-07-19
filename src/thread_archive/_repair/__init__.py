"""``archive fix-import`` — user-run agent repair of a drifted provider import.

The archive's parsers rot on the provider's schedule, not the maintainer's: a
harness update changes its transcript format and the importer soft-degrades
(ledgers, coverage, the search-result notice) until someone fixes it. The user
whose machine has the drift also has the samples, a harness with edit rights,
and the motivation — so the fix runs *there*: this module scaffolds an
override-plugin patch (:mod:`.scaffold`), spawns a headless ``claude`` bounded
to that scaffold to write the parse logic, and gates the result through
deterministic activation (:mod:`.activate`) — tests green, then enabled, then
the ledger-driven re-import recovers everything the broken parser consumed
(:mod:`.reimport`).

The spawn copies the ``archive curate`` shape: repo-hosted prompt text loaded
via ``importlib.resources``, ``--print`` + ``--strict-mcp-config``, group-kill
on timeout. Two deliberate differences. It runs in the *foreground* with
stdout/stderr inherited — the user invoked it and watches it work; this is a
repair they asked for, not scheduled machinery. And its strict MCP config is
**empty**: the fix needs only the files the scaffold laid out in its cwd, and
an unattended ``bypassPermissions`` run must not inherit whatever MCP servers
the operator's own config would otherwise inject. The real containment is the
activation gate — nothing the spawned agent claims enables a patch; only the
scaffold's tests passing in a subprocess does.

Patches are temporary by default (retired by the next self-update —
:mod:`.retire`) and pinnable for "I always want mine". Every lifecycle
transition lands in ``patch-log.jsonl`` (:mod:`.ledger`).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from importlib import resources
from typing import Optional

from .._config import load_config, resolve_paths
from .activate import ActivationError, activate, set_pinned  # noqa: F401 — package API
from .reimport import reimport_source  # noqa: F401 — package API
from .retire import retire_patches  # noqa: F401 — package API
from .scaffold import plugin_dir, scaffold  # noqa: F401 — package API

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "xhigh"
# A parser fix iterates (diagnose, fixture, implement, test, activate) — twice
# the curation budget.
TIMEOUT_S = 3600
PROMPT_FILE = "fix_import.md"


def repair_settings(home: Optional[str] = None) -> tuple[str, str]:
    """``(model, effort)`` for the repair spawn, from config ``repair.model`` /
    ``repair.effort``. Fail-soft like curation's: bad types fall back to the
    defaults; an explicitly empty effort omits the flag (for a CLI that doesn't
    know it)."""
    cfg = load_config(home).get("repair")
    cfg = cfg if isinstance(cfg, dict) else {}
    model = cfg.get("model", DEFAULT_MODEL)
    model = model if isinstance(model, str) and model else DEFAULT_MODEL
    effort = cfg.get("effort", DEFAULT_EFFORT)
    if effort is None:
        effort = ""
    elif not isinstance(effort, str):
        effort = DEFAULT_EFFORT
    return model, effort


def prompt_text(provider_name: str) -> str:
    """The packaged repair protocol plus this run's specifics."""
    base = (resources.files(__package__) / PROMPT_FILE).read_text(encoding="utf-8")
    return (
        f"{base.rstrip()}\n\n## This run\n\n"
        f"- provider: `{provider_name}`\n"
        f"- scaffold: the current working directory (evidence.md, quirks.md, "
        f"samples/, fixtures/, the patch module, test_patch.py)\n"
        f"- exit bar: `python -m pytest . -q` green, then "
        f"`archive fix-import {provider_name} --activate` succeeding\n"
    )


def run(
    provider_name: str,
    home: Optional[str] = None,
    *,
    timeout: int = TIMEOUT_S,
    claude: Optional[str] = None,
) -> int:
    """Scaffold, spawn the repair agent, report whether the patch went active.

    Returns 0 when activation happened (the config entry is enabled when the
    spawn exits), 1 otherwise — unlike a scheduled drain, the invoking user is
    present, and "the fix didn't land" is an exit code they act on. ``claude``
    is injectable for tests, as in curation."""
    from .._curation import resolve_claude

    target = scaffold(provider_name, home)

    cli = claude or resolve_claude()
    if not cli:
        logger.error(
            "no `claude` CLI found — the scaffold is ready at %s; fix by hand or "
            "with your own agent, then run `archive fix-import %s --activate`",
            target, provider_name,
        )
        return 1

    paths = resolve_paths(home)
    mcp_path = paths.home / "repair-mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}, indent=2), encoding="utf-8")

    model, effort = repair_settings(home)
    args = [
        cli,
        "--print",
        # Headless run: nobody approves tool calls mid-flight. Containment is
        # the empty strict MCP surface plus the activation gate — the spawn
        # cannot enable its own patch; only a green test suite can.
        "--permission-mode", "bypassPermissions",
        "--model", model,
        *(["--effort", effort] if effort else []),
        "--mcp-config", str(mcp_path),
        "--strict-mcp-config",
        prompt_text(provider_name),
    ]
    logger.info(
        "%s: launching repair agent (model=%s%s, timeout=%ds) — output follows",
        provider_name, model, f", effort={effort}" if effort else "", timeout,
    )
    try:
        proc = subprocess.Popen(args, cwd=str(target), start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            logger.error(
                "%s: repair agent hit the %ds budget — the scaffold keeps its "
                "progress; re-run to continue, or finish by hand and "
                "`--activate`", provider_name, timeout,
            )
    except Exception:
        logger.exception("%s: repair agent launch failed", provider_name)

    entry = (load_config(home).get("providers") or {}).get(provider_name) or {}
    if isinstance(entry, dict) and entry.get("enabled"):
        logger.info("%s: patch is ACTIVE — coverage clears on its next pass", provider_name)
        return 0
    logger.warning(
        "%s: patch is not active. Inspect %s, then re-run `archive fix-import %s` "
        "or activate by hand with --activate.", provider_name, target, provider_name,
    )
    return 1
