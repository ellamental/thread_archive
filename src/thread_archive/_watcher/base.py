"""Source-watcher interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, TypeVar, Union

T = TypeVar("T")
F = TypeVar("F")


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


def fingerprint_poll(
    targets: Iterable[T],
    seen: dict[str, F],
    *,
    probe: Callable[[T], Union[tuple[str, F], "WatchResult", None]],
    work: Callable[[T], "WatchResult"],
    on_error: Callable[[T, Exception], "WatchResult"],
) -> "WatchResult":
    """One fingerprint-skip poll pass, shared by the file- and db-scan sources.

    For each target, ``probe`` returns ``(key, fingerprint)`` to consider it, a
    :class:`WatchResult` to fold in and skip (a pre-work error or skip that still
    reports), or ``None`` to skip silently. A target whose fingerprint still
    matches ``seen`` is counted (``sources_checked=1``) and skipped; otherwise
    ``work`` runs and its fingerprint is advanced **only after ``work`` returns**
    — a raise routes to ``on_error`` and leaves the fingerprint stale so the next
    poll retries the target. Fingerprints for targets no longer present are pruned.
    ``seen`` is the caller's per-instance fingerprint cache, mutated in place."""
    result = WatchResult()
    seen_this_poll: set[str] = set()
    for target in targets:
        probed = probe(target)
        if probed is None:
            continue
        if isinstance(probed, WatchResult):
            result = result + probed
            continue
        key, fingerprint = probed
        seen_this_poll.add(key)
        if seen.get(key) == fingerprint:
            result = result + WatchResult(sources_checked=1)
            continue
        try:
            done = work(target)
        except Exception as e:  # noqa: BLE001 — one bad target must not stop the poll
            result = result + on_error(target, e)
            continue
        seen[key] = fingerprint  # advance only after a successful unit of work
        result = result + done
    if seen_this_poll:
        for key in [k for k in seen if k not in seen_this_poll]:
            del seen[key]
    return result


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

    def store_paths(self) -> Iterator[Path]:
        """The store's constituent files, for preservation snapshots (the drift
        quarantine, :mod:`.drift_snapshot`). Base form: none — a watcher that
        cannot enumerate its store cheaply yields nothing and its source is
        snapshot-exempt."""
        return iter(())
