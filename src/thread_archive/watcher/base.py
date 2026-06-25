"""Source-watcher interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class WatchResult:
    """Result of a single poll iteration."""

    sources_checked: int = 0
    items_imported: int = 0
    events_created: int = 0
    errors: list[str] = field(default_factory=list)

    def __add__(self, other: "WatchResult") -> "WatchResult":
        return WatchResult(
            sources_checked=self.sources_checked + other.sources_checked,
            items_imported=self.items_imported + other.items_imported,
            events_created=self.events_created + other.events_created,
            errors=self.errors + other.errors,
        )


class SourceWatcher(ABC):
    """Watches one kind of local AI-tool store and imports new content."""

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
