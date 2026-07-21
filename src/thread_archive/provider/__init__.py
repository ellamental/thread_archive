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

## Resolving a per-OS store location

A watcher points at where its harness keeps transcripts. A CLI harness uses a
home-relative path (``~/.codex``) that ports across OSes for free; a desktop or
Electron one (Cursor, the Claude app) sits under a different per-user root on
each OS. Resolve that root with :func:`app_data_dir` rather than branching on the
platform inside your own watcher — it is the one place the macOS/Linux/Windows
mapping lives, so a provider that uses it gains a new OS the day archive learns
it, and a wrong guess on an unknown OS leaves the source dormant rather than
crashing ingest.

## Knowledge about a provider belongs to the provider

Everything archive needs to know about a source is declared on its descriptor,
including the parts that surface far from the importer: how its stored events
should be *displayed* (``render``) and how a session id sits inside its
``source_id`` (``session_id_separators``). Generic code asks the registry rather
than carrying a list of provider names, so a plugin reaches those seams on the
same terms a built-in does.

That is a correctness property, not tidiness. A rule about one provider's format
applied to every provider silently corrupts the others — a prompt-unwrapping
rule eats a turn that merely quotes the wrapper, an id-composition rule resolves
a uuid to a thread that merely ends the same way.

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
from typing import Any, Callable, Container, Literal, Optional

from .._importers._events import (
    assemble_events,
    log_parse_validation,
    preserve_unmodeled_fields,
)
from .._importers._line_stream import line_stream_importer
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

# Claude Code's format is deliberately published for reuse — a harness writing its
# shape gets the parser wholesale. The helper is defined with that provider and
# re-exported here, the same way the watcher bases are.
from .._importers.claude_code import claude_code_line_stream
from .._store import ImportState, get_session
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
from .._watcher.paths import app_data_dir
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


class _DefaultView:
    """The sentinel type — see :data:`DEFAULT_VIEW`."""

    def __repr__(self) -> str:
        return "DEFAULT_VIEW"


#: Returned by :attr:`RenderPolicy.block` to decline a block: the reader falls
#: back to its own default view. Distinct from ``None``, which means *hide this
#: block*. A policy that claims some of its provider's block types and not
#: others needs all three answers, so they are three distinct return values.
DEFAULT_VIEW = _DefaultView()

#: ``(block_type, data, rendered_text) -> (label, text) | None | DEFAULT_VIEW``.
BlockRenderer = Callable[[str, Any, Container[str]], Any]


@dataclass(frozen=True)
class RenderPolicy:
    """How a reader presents this provider's stored events.

    Rendering is the one place a provider's quirks legitimately reach outside
    the importer: a harness that wraps the operator's prompt in a tag, or writes
    its transcript twice, produces events that are correct as *stored truth* and
    misleading as *displayed conversation*. Both readers (the CLI/MCP transcript
    and the web viewer) apply the policy of the thread's own provider, so a quirk
    can never reshape another provider's turns.

    A policy is presentation only. It never changes what was stored, and the
    untouched payload stays available in the event log and the viewer's raw view.

    ``user_content(content) -> str`` rewrites a user turn for display — unwrapping
    a harness's prompt wrapper, say. It must be **content-preserving or
    explicitly lossy for one known shape**: it runs on every user turn of that
    provider, including turns that merely quote the shape it looks for.

    ``block(block_type, data, rendered_text) -> (label, text) | None |``
    :data:`DEFAULT_VIEW` decides what a preserved ``content_block`` event looks
    like: a rendered ``(label, text)``, ``None`` to hide it, or
    :data:`DEFAULT_VIEW` to take the reader's default flattening. ``rendered_text``
    holds the stripped text of every turn the modeled path already shows, so a
    provider that records its transcript twice can suppress the copy by content
    rather than by kind.
    """

    user_content: Optional[Callable[[str], str]] = None
    block: Optional[BlockRenderer] = None


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
    #: How this provider presents its stored events to a reader. Leave unset and
    #: the readers render the events as they are — the right answer for almost
    #: every provider.
    render: Optional[RenderPolicy] = None
    #: The characters that separate a prefix from the session id inside this
    #: provider's ``source_id``, when it composes one. Claude Code stores
    #: ``{project}:{uuid}`` and declares ``(":",)``; codex stores
    #: ``rollout-{timestamp}-{uuid}`` and declares ``("-",)``. Empty — the
    #: default — means the ``source_id`` *is* the session id.
    #:
    #: It is what lets an agent holding only a bare session uuid resolve the
    #: thread it names. Declaring it narrowly matters: every separator any
    #: provider declares is tried when resolving a reference whose provider is
    #: unknown, so a broad one invites a uuid resolving to another provider's
    #: thread that merely ends the same way.
    session_id_separators: tuple[str, ...] = ()
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
        if not all(isinstance(sep, str) and sep for sep in self.session_id_separators):
            raise ValueError(
                f"provider {self.name!r}: session_id_separators must be non-empty strings; "
                f"declare () when the source_id is the session id"
            )


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


def builtin(name: str) -> Provider:
    """The built-in :class:`Provider` named ``name``, bypassing plugin overrides.

    The starting point for an override plugin: shadowing a built-in usually means
    changing one thing about it — ``dataclasses.replace(builtin("codex"),
    parser_config=...)`` — not rebuilding the descriptor from scratch. Reading
    through the registry instead would hand an override plugin *itself* back
    while it loads (a cycle); this reads the built-in set directly. Raises
    ``KeyError`` for a name no built-in claims.
    """
    from .._providers.builtins import builtin_providers

    for provider in builtin_providers():
        if provider.name == name:
            return provider
    raise KeyError(f"no built-in provider named {name!r}")


__all__ = [
    # Descriptor
    "Provider",
    "ExportSpec",
    "ImporterKind",
    "resolve_provider",
    "builtin",
    # Rendering
    "RenderPolicy",
    "BlockRenderer",
    "DEFAULT_VIEW",
    # Watcher contract
    "SourceWatcher",
    "WatchResult",
    "SourceDiscovery",
    "fingerprint_poll",
    "FileSessionWatcher",
    "RglobWatcher",
    "DbScanWatcher",
    "stat_discovery",
    "app_data_dir",
    # Importer construction
    "line_stream_importer",
    "claude_code_line_stream",
    "IncrementalImportResult",
    "DbScanResult",
    # Import primitives
    "assemble_events",
    "log_parse_validation",
    "preserve_unmodeled_fields",
    "record_skip",
    "get_session",
    # Thread + watermark state
    "create_thread",
    "discard_new_thread",
    "get_thread_by_source",
    "adopt_if_unwatermarked",
    "get_import_state",
    "upsert_import_state",
    # The watermark row itself, for a db-scan importer that annotates the value
    # ``get_import_state`` hands back (both of archive's do).
    "ImportState",
    "set_thread_models_from_events",
    "update_thread_title",
    "update_thread_description",
    # Reading source files
    "read_source_bytes",
    "parse_session_lines",
    "parse_session_lines_counted",
    "sanitize_payload",
]
