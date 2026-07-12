"""Source-watcher interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WatchResult:
    """Result of a single poll iteration.

    ``lines_processed`` / ``parse_errors`` carry the file sources' yield
    accounting (new source lines fed to the importer; lines dropped as
    unparseable) so the watch loop's health record can expose capture-side
    loss — the DB-scan sources have no line notion and leave them 0."""

    sources_checked: int = 0
    items_imported: int = 0
    events_created: int = 0
    lines_processed: int = 0
    parse_errors: int = 0
    errors: list[str] = field(default_factory=list)

    def __add__(self, other: "WatchResult") -> "WatchResult":
        return WatchResult(
            sources_checked=self.sources_checked + other.sources_checked,
            items_imported=self.items_imported + other.items_imported,
            events_created=self.events_created + other.events_created,
            lines_processed=self.lines_processed + other.lines_processed,
            parse_errors=self.parse_errors + other.parse_errors,
            errors=self.errors + other.errors,
        )


@dataclass
class SourceDiscovery:
    """What a dry-run look at one source's store found — stat-only, no content
    parse, no import. ``items`` is a session/conversation count where the store
    exposes one cheaply (per-session-file sources), else ``None``; timestamps
    are file mtimes (unix seconds), the honest cheap proxy for activity range."""

    name: str
    available: bool
    items: Optional[int] = None
    bytes: int = 0
    earliest: Optional[float] = None
    latest: Optional[float] = None


class SourceWatcher(ABC):
    """Watches one kind of local AI-tool store and imports new content."""

    #: True when the store's file mtimes move only on content writes (per-session
    #: transcript files), so "store mtime newer than the newest archived event" is
    #: evidence of missed capture. False for live SQLite stores, whose mtimes churn
    #: without new conversations (app launches, vacuum) — the capture-coverage
    #: check exempts those from its staleness comparison.
    store_mtime_tracks_content: bool = False

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    @abstractmethod
    def poll(self) -> WatchResult:
        """Poll for changes and import new content (catching per-item errors)."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """True if this source's paths exist on this system."""
        ...

    def discover(self) -> SourceDiscovery:
        """Cheap dry-run report of what this source's store holds (see
        :class:`SourceDiscovery`). Base form: availability only; watchers that
        can stat their stores cheaply override with counts/sizes/ranges."""
        return SourceDiscovery(name=self.source_name, available=self.is_available())
