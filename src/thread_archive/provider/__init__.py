"""The public provider API — the seam a provider plugin is written against.

Everything else in ``thread_archive`` is private and free to churn. This module
is the contract: a provider defined outside the package imports from here, and
archive keeps these names working.

## What a provider is

A :class:`Provider` is one descriptor tying together the pieces archive needs to
preserve a source: where its transcripts live (``watcher``), how to turn them
into events (``importer``), what its parser knows about its own format
(``parser_config``), and how it presents itself to an operator (``label``).
Archive builds its own providers from exactly this descriptor, so a plugin is
not a second-class citizen — there is one path.

## Declaring one

Publish a ``thread_archive.providers`` entry point resolving to a
:class:`Provider` (or to a zero-argument callable returning one, for a
descriptor that shouldn't be built at import time)::

    # pyproject.toml
    [project.entry-points."thread_archive.providers"]
    myharness = "myharness_archive:PROVIDER"

Install that distribution into the same environment as archive and the watcher
daemon picks it up on its next start — the daemon runs from archive's own venv,
so an entry point registered there is visible with no further wiring.

For a provider that isn't packaged — one under development, one living beside
the harness it serves — declare it in ``<home>/config.json`` instead::

    {"providers": {"myharness": {"module": "myharness_archive:PROVIDER",
                                 "path": "~/src/myharness/archive-plugin/src"}}}

``path`` (optional) goes on ``sys.path`` before the import. Both mechanisms feed
one registry and are equivalent downstream; entry points are how a provider
ships, config declaration is how one is developed.

Discovery is fail-soft in both directions: a plugin that raises on import is
logged and skipped, never fatal — one broken provider must not take down ingest
for every other source.

## Three importer shapes

``kind`` tells archive how ``archive import --provider <name>`` reaches the
importer, and nothing else — a provider's watcher can do whatever it likes.

- ``"line-stream"`` — one JSONL file per session. ``importer(path, source_id)``.
  Build it with :func:`line_stream_importer`, which supplies the cursor, the
  watermark, thread lifecycle and atomic commit around five callbacks.
- ``"db-scan"`` — one live SQLite store holding many sessions.
  ``importer(db_path)`` returns a :class:`DbScanResult`.
- ``"none"`` — not reachable by path (a dispatch needing more than a path, or a
  provider fed only by account exports). Its watcher drives it directly.

Orthogonally, a provider may set ``export`` to accept downloaded account
exports dropped into ``<home>/dumps/``. A source can have both — the Grok CLI
watcher and xAI account exports are one provider.

## Reusing a built-in parser

A harness that writes another provider's transcript shape should reuse that
parser rather than copy it. :func:`claude_code_line_stream` returns an importer
over Claude Code's format under your own source name, so threads carry your
provenance instead of masquerading. Pair it with
``CLAUDE_CODE_CONFIG.derive(...)`` (see :mod:`thread_archive.provider.parse`) to
add the line types and fields your harness emits that Claude Code doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .._importers._events import assemble_events, log_parse_validation
from .._importers._line_stream import import_line_stream_session
from .._importers._read import (
    parse_session_lines,
    parse_session_lines_counted,
    read_source_bytes,
    sanitize_payload,
)
from .._importers._result import DbScanResult, IncrementalImportResult
from .._importers._skip_ledger import record_skip
from .._importers._state import (
    adopt_if_unwatermarked,
    create_thread,
    discard_new_thread,
    get_import_state,
    get_thread_by_source,
    set_thread_models_from_events,
    update_thread_description,
    update_thread_title,
    upsert_import_state,
)
from .._store import get_session
from .._watcher.base import (
    SourceDiscovery,
    SourceWatcher,
    WatchResult,
    fingerprint_poll,
)
from .._watcher.sources import (
    DbScanWatcher,
    FileSessionWatcher,
    RglobWatcher,
    stat_discovery,
)
from .parse import ProviderConfig

ImporterKind = Literal["line-stream", "db-scan", "none"]


@dataclass(frozen=True)
class ExportSpec:
    """A provider's ability to import a downloaded account export.

    An account export is a different shape from a live store: a ZIP (or unzipped
    directory) holding *all* of a user's conversations, downloaded by hand. The
    export-drop watcher offers each bundle left in ``<home>/dumps/`` to every
    registered spec in registry order and the first ``detect`` to claim it wins,
    so a ``detect`` must be cheap and must not claim a bundle it cannot import —
    a false claim quarantines another provider's export.

    ``importer(path, *, force=False)`` imports every conversation in the bundle
    as its own thread and returns a result carrying ``processed`` / ``imported``
    / ``skipped`` / ``errored`` / ``events_created``.

    ``kind`` is the retention slug: a cleanly-imported bundle is kept under
    ``dumps/imported/<kind>/`` as the recovery copy, and the next clean import of
    the same kind supersedes it. It is separate from the provider name because a
    provider can be fed by an export whose vendor name differs from the source
    threads land under, and because changing it strands the copies already
    retained under the old slug.
    """

    detect: Callable[[Path], bool]
    importer: Callable[..., Any]
    label: str
    kind: str


@dataclass(frozen=True)
class Provider:
    """One preservable source of conversations.

    ``name`` is the identity threads are stored under (``Thread.source``) and the
    key for config opt-out, ``archive import --provider``, and search filters. It
    is permanent: changing it orphans every thread already imported under the old
    one. Lowercase, hyphenated.
    """

    name: str
    label: str
    watcher: Optional[Callable[[], SourceWatcher]] = None
    importer: Optional[Callable[..., Any]] = None
    kind: ImporterKind = "none"
    parser_config: Optional[ProviderConfig] = None
    #: A parser class to register under this provider's name, for a provider that
    #: brings its own. Leave unset when messages are hand-built (most providers
    #: do this) or when reusing another provider's parser — for the latter, name
    #: it in ``parser_id``.
    parser: Optional[type] = None
    #: Which registered parser reads this provider's transcripts, when one does.
    #: Usually the provider's own name; for a harness reusing another provider's
    #: format it is *that* provider's name. Source identity and parser identity
    #: are separate on purpose: a harness writing Claude Code's shape must be
    #: parsed as Claude Code while being stored, attributed, and validated as
    #: itself. Tooling that re-parses transcripts groups by this.
    parser_id: Optional[str] = None
    export: Optional[ExportSpec] = None
    #: True for a watcher that is archive's own machinery rather than a store an
    #: operator chose to have — the drop zone, a recovery pass over another
    #: source. Setup presents provider sources only; mechanisms ride along.
    mechanism: bool = False
    #: Another provider whose enablement this one follows: disabling that source
    #: disables this one too. A recovery pass over another source's store has no
    #: independent meaning.
    follows: Optional[str] = None
    #: Poll order within one watch pass. A watcher that recovers what another
    #: source persists must run after it, so the primary import establishes its
    #: dedup keys first and the recovery pass writes only what is genuinely lost.
    order: int = 100
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError(f"provider name must be non-empty and unpadded: {self.name!r}")
        if self.kind != "none" and self.importer is None:
            raise ValueError(
                f"provider {self.name!r} declares kind={self.kind!r} but has no importer"
            )
        if self.parser_config is not None and self.parser_config.provider_name != self.name:
            raise ValueError(
                f"provider {self.name!r} carries a parser_config named "
                f"{self.parser_config.provider_name!r} — a config is registered under its own "
                f"provider_name, so the two must match or validation resolves the wrong config"
            )
        if self.follows == self.name:
            raise ValueError(f"provider {self.name!r} cannot follow itself")


def line_stream_importer(
    source: str,
    *,
    has_importable_content: Callable[[list[dict]], bool],
    make_title: Callable[..., str],
    import_lines: Callable[..., tuple[int, Optional[str]]],
    prepare: Optional[Callable[..., Any]] = None,
    make_source_metadata: Optional[Callable[[Any], Optional[dict]]] = None,
    not_found_msg: Optional[str] = None,
) -> Callable[..., IncrementalImportResult]:
    """An importer for a one-JSONL-file-per-session provider, from five callbacks.

    Everything an incremental import has to get right is supplied: reading the
    file once and parsing from that buffer, the append proof that re-imports from
    zero if the bytes under the cursor changed, thread creation and its discard
    when a poll turns out to hold no events, the skip ledger, the watermark, and
    the single transaction all of it commits in. A dedup key collapses anything
    already held, so a re-import costs work but never duplicates.

    The callbacks, in the order they run:

    - ``prepare(all_lines, path) -> ctx`` — optional per-import context, passed
      to the rest. Derive whole-file state here (ambient model, call maps): the
      incremental path hands you every line, not only the new ones.
    - ``has_importable_content(new_lines) -> bool`` — is there anything worth a
      thread yet? False on a first poll records a skip and creates nothing, so a
      store's bookkeeping-only preamble doesn't leave empty threads behind.
    - ``make_title(all_lines, ctx) -> str`` — the new thread's title.
    - ``make_source_metadata(ctx) -> dict | None`` — optional provenance stored
      on the thread.
    - ``import_lines(session, thread_id, all_lines, new_lines, ctx)`` — build
      messages and write them, returning ``(events_created, last_message_uuid)``.
      End in :func:`assemble_events`; it owns dedup, stream ids, monotonic
      timestamps, the truth-log write and search indexing.

    The returned importer is ``(session_path, source_id, *, session=None) ->``
    :class:`IncrementalImportResult` — the shape a file watcher expects. Passing
    ``session`` hands commit control to the caller.
    """

    def _import(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
        return import_line_stream_session(
            session_path=session_path,
            source_id=source_id,
            source=source,
            session=session,
            prepare=prepare,
            has_importable_content=has_importable_content,
            make_title=make_title,
            make_source_metadata=make_source_metadata,
            import_lines=import_lines,
            not_found_msg=not_found_msg or f"{source} transcript not found: {session_path}",
        )

    _import.__name__ = f"import_{source.replace('-', '_')}_session_incremental"
    _import.__doc__ = f"Import one {source} transcript into the event log."
    return _import


def claude_code_line_stream(source: str) -> Callable[..., IncrementalImportResult]:
    """A line-stream importer over Claude-Code-shaped JSONL, under ``source``.

    A harness that writes Claude Code's transcript shape — ``user`` /
    ``assistant`` lines, extra line kinds the parser preserves verbatim — reuses
    Claude Code's parser wholesale instead of duplicating it. Threads land under
    ``source`` with the harness's own provenance, and idempotence, the truth-log
    seam and incremental watermarking all come from the shared path unchanged.

    Register a ``ProviderConfig`` derived from ``CLAUDE_CODE_CONFIG`` alongside
    it (``Provider.parser_config``) so the extra line types and fields the
    harness emits are known rather than reported as Claude Code drift.

    Claude Code's own continuation/fork merging and its ``.context.jsonl``
    sidecar stay out: both key off conventions specific to Claude Code's store,
    and applying them to another harness's ids would merge unrelated threads.
    """
    from .._importers.claude_code import import_session_incremental

    def _import(session_path, source_id: str, *, session=None) -> IncrementalImportResult:
        return import_session_incremental(
            session_path, source_id, source=source, session=session
        )

    _import.__name__ = f"import_{source.replace('-', '_')}_session_incremental"
    _import.__doc__ = (
        f"Import one Claude-Code-shaped {source} transcript into the event log."
    )
    return _import


def resolve_provider(obj: object) -> Provider:
    """A :class:`Provider` from either a descriptor or a factory returning one.

    Takes ``object`` because its real callers hand it whatever an entry point or
    a config-named attribute resolved to — unknown by construction. Checking here
    is what turns a plugin's mistake into one skipped provider with a clear log
    line instead of an obscure failure further in.
    """
    if isinstance(obj, Provider):
        return obj
    if callable(obj):
        built = obj()
        if isinstance(built, Provider):
            return built
        raise TypeError(f"provider factory returned {type(built).__name__}, not a Provider")
    raise TypeError(f"expected a Provider or a callable returning one, got {type(obj).__name__}")


__all__ = [
    # Descriptor
    "Provider",
    "ExportSpec",
    "ImporterKind",
    "resolve_provider",
    # Watcher contract
    "SourceWatcher",
    "WatchResult",
    "SourceDiscovery",
    "fingerprint_poll",
    "FileSessionWatcher",
    "RglobWatcher",
    "DbScanWatcher",
    "stat_discovery",
    # Importer construction
    "line_stream_importer",
    "claude_code_line_stream",
    "IncrementalImportResult",
    "DbScanResult",
    # Import primitives
    "assemble_events",
    "log_parse_validation",
    "record_skip",
    "get_session",
    # Thread + watermark state
    "create_thread",
    "discard_new_thread",
    "get_thread_by_source",
    "adopt_if_unwatermarked",
    "get_import_state",
    "upsert_import_state",
    "set_thread_models_from_events",
    "update_thread_title",
    "update_thread_description",
    # Reading source files
    "read_source_bytes",
    "parse_session_lines",
    "parse_session_lines_counted",
    "sanitize_payload",
]
