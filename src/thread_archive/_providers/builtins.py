"""Archive's own providers, as :class:`~thread_archive.provider.Provider` descriptors.

These go through the same public API a plugin does. That is the point: the
built-ins are the API's proof, so a seam they need and a plugin can't reach is a
bug in the API rather than a private convenience. Everything downstream — the
watcher set, importer dispatch, setup's source list, parser-config registration,
export-drop classification — reads this one list.

Cowork and Claude Science are worth noting: each reuses ``ClaudeCodeParser``
under its own source name, and each carries a ``ProviderConfig`` derived from
Claude Code's rather than sharing it, so drift is attributed to the harness that
actually emitted it. A plugin reusing a built-in parser takes the same shape.
"""

from __future__ import annotations

from pathlib import Path

from .._importers import (
    import_antigravity_session_incremental,
    import_codex_session_incremental,
    import_cursor_db,
    import_grok_session_incremental,
    import_opencode_db,
    import_session_incremental,
)
from .._importers.grok import display_user_content as grok_display_user_content
from ..provider import ExportSpec, Provider, RenderPolicy
from ..provider.parse import CHATGPT_CONFIG, CLAUDE_CODE_CONFIG, CLAUDE_CONFIG
from ._codex_render import CODEX_RENDER

# Cowork and Claude Science share Claude Code's parser under their own identity.
# Same shape, own provenance: a config named for the harness keeps its drift out
# of Claude Code's ledger without claiming the format differs.
COWORK_CONFIG = CLAUDE_CODE_CONFIG.derive("cowork")
CLAUDE_SCIENCE_CONFIG = CLAUDE_CODE_CONFIG.derive(
    "claude-science",
    # The Science agents call Anthropic's server-side tools, so a turn can carry
    # the call and its results as their own blocks. Preserved raw, and declared
    # here rather than on Claude Code's config — which has never emitted them,
    # and should still warn if it starts.
    expected_block_types={"server_tool_use", "web_search_tool_result"},
)


def _export_spec(kind: str, label: str, importer_name: str) -> ExportSpec:
    """An :class:`ExportSpec` over the shared account-export classifier.

    claude.ai and ChatGPT exports both ship a root ``conversations.json``, so
    neither can be recognized without ruling the other out. The classifier holds
    that mutual disambiguation in one place; each provider's ``detect`` asks it
    the same question and keeps only its own answer, so the three stay exclusive
    by construction while remaining separately registered.

    Both callables resolve their target on the module at call time rather than
    capturing it: the descriptor is built once per process, and a captured
    function would freeze out any later rebinding of it.
    """

    def detect(path: Path) -> bool:
        from .._importers import exports

        return exports.classify_export(path) == kind

    def importer(path, **kwargs):
        from .._importers import exports

        return getattr(exports, importer_name)(path, **kwargs)

    return ExportSpec(detect=detect, importer=importer, label=label, kind=kind)


def builtin_providers() -> list[Provider]:
    """Every provider archive ships, in poll order."""
    from .._watcher.export_drop import ExportDropWatcher
    from .._watcher.exthost import ExthostWatcher
    from .._watcher.sources import (
        ClaudeCodeWatcher,
        ClaudeScienceWatcher,
        CoworkWatcher,
        antigravity_watcher,
        codex_watcher,
        cursor_watcher,
        grok_watcher,
        opencode_watcher,
    )

    return [
        Provider(
            name="claude-code",
            label="Claude Code",
            watcher=ClaudeCodeWatcher,
            importer=import_session_incremental,
            kind="line-stream",
            parser_config=CLAUDE_CODE_CONFIG,
            parser_id="claude-code",
            # source_id is "{project}:{session uuid}".
            session_id_separators=(":",),
            order=10,
        ),
        Provider(
            name="codex",
            label="Codex",
            watcher=codex_watcher,
            importer=import_codex_session_incremental,
            kind="line-stream",
            # source_id is "rollout-{timestamp}-{session uuid}".
            session_id_separators=("-",),
            render=CODEX_RENDER,
            order=20,
        ),
        Provider(
            name="grok",
            label="Grok",
            watcher=grok_watcher,
            importer=import_grok_session_incremental,
            kind="line-stream",
            render=RenderPolicy(user_content=grok_display_user_content),
            # The Grok CLI and xAI account exports are one source: CLI sessions
            # arrive live, web conversations only when an export is dropped.
            export=_export_spec("xai", "xAI/Grok", "import_xai_export"),
            order=30,
        ),
        Provider(
            name="antigravity",
            label="Antigravity",
            watcher=antigravity_watcher,
            importer=import_antigravity_session_incremental,
            kind="line-stream",
            order=40,
        ),
        Provider(
            name="cursor",
            label="Cursor",
            watcher=cursor_watcher,
            importer=import_cursor_db,
            kind="db-scan",
            order=60,
        ),
        Provider(
            name="opencode",
            label="OpenCode",
            watcher=opencode_watcher,
            importer=import_opencode_db,
            kind="db-scan",
            order=70,
        ),
        Provider(
            name="cowork",
            label="Cowork",
            watcher=CoworkWatcher,
            # Dispatch needs the sibling metadata path the watcher resolves per
            # session, so there is no (path, source_id) form to expose.
            kind="none",
            parser_config=COWORK_CONFIG,
            parser_id="claude-code",
            # source_id is "{org}:{workspace}:{session uuid}".
            session_id_separators=(":",),
            order=80,
        ),
        Provider(
            name="claude-science",
            label="Claude Science",
            watcher=ClaudeScienceWatcher,
            # Scans per-org DBs discovered each poll, not one fixed path, so
            # dispatch takes an org uuid the watcher supplies.
            kind="none",
            parser_config=CLAUDE_SCIENCE_CONFIG,
            parser_id="claude-code",
            # source_id is "{org uuid}:{session uuid}".
            session_id_separators=(":",),
            order=90,
        ),
        Provider(
            name="claude",
            label="claude.ai",
            # No live store to watch — claude.ai conversations reach the archive
            # only through a downloaded account export.
            kind="none",
            parser_config=CLAUDE_CONFIG,
            parser_id="claude",
            export=_export_spec("claude", "claude.ai", "import_claude_ai_export"),
            order=95,
        ),
        Provider(
            name="chatgpt",
            label="ChatGPT",
            kind="none",
            parser_config=CHATGPT_CONFIG,
            parser_id="chatgpt",
            export=_export_spec("chatgpt", "ChatGPT", "import_chatgpt_export"),
            order=96,
        ),
        Provider(
            name="export-drop",
            label="Account exports",
            watcher=ExportDropWatcher,
            kind="none",
            mechanism=True,
            # Reads only the archive's own <home>/dumps drop zone, so it is
            # consented by construction rather than a store an operator chose.
            order=200,
        ),
        Provider(
            name="cc-exthost",
            label="Claude Code (VS Code recovery)",
            watcher=ExthostWatcher,
            kind="none",
            mechanism=True,
            follows="claude-code",
            # Last: the JSONL sources import each session's persisted messages
            # first and establish their dedup keys, leaving this pass only the
            # steering messages that were genuinely lost.
            order=300,
        ),
    ]
