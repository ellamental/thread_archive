"""One lazily-constructed heavy model + its cached-failure flag.

Shared by :mod:`.embed` and :mod:`.rerank`, whose torch models are far too
heavy to construct twice (or in tests at all). The slot IS the public state
seam: ``model`` and ``load_failed`` are plain attributes, so tests inject a
scripted model or a cached failure through the front door
(``monkeypatch.setattr(embed.SLOT, "model", fake)``) instead of faking the
owning module's internals.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class ModelSlot:
    """Double-checked lazy holder: construct once, cache the model or the failure."""

    def __init__(self) -> None:
        self.model: object | None = None
        self.load_failed = False
        # Serializes construction so a background warm and a concurrent first
        # query race to a single build, not two heavy loads at once.
        self._lock = threading.Lock()

    def get(
        self,
        construct: Callable[[], object],
        on_error: Callable[[Exception], None],
    ) -> object | None:
        """The cached model, constructing it on first call.

        Returns None after a failed construction — the failure is cached too,
        so it costs one attempt, not one per query. ``on_error`` observes the
        exception exactly once.
        """
        if self.model is not None:
            return self.model
        if self.load_failed:
            return None
        with self._lock:
            if self.model is not None:
                return self.model
            if self.load_failed:
                return None
            try:
                self.model = construct()
                return self.model
            except Exception as e:  # noqa: BLE001
                self.load_failed = True
                on_error(e)
                return None
