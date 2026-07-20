"""One lazily-constructed heavy model + its cached-failure flag.

Shared by :mod:`.embed` and :mod:`.rerank`, whose torch models are far too heavy
to construct twice. The slot takes its loader at construction, so it holds any
model, and accepts an already-loaded one — a caller that already has a
SentenceTransformer hands it in rather than paying for a second copy.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, Optional, TypeVar

M = TypeVar("M")


def _ignore(_e: Exception) -> None:
    """Default failure observer: the slot's cached degrade is the whole contract."""


class ModelSlot(Generic[M]):
    """Double-checked lazy holder: construct once, cache the model or the failure.

    ``construct`` builds the model on first :meth:`get`; ``on_error`` observes a
    construction failure exactly once. ``model`` pre-fills the slot, so a
    ready-made model is used as-is and ``construct`` is never called.
    """

    def __init__(
        self,
        construct: Callable[[], M],
        on_error: Callable[[Exception], None] = _ignore,
        *,
        model: Optional[M] = None,
    ) -> None:
        self._construct = construct
        self._on_error = on_error
        self.model = model
        self.load_failed = False
        # Serializes construction so a background warm and a concurrent first
        # query race to a single build, not two heavy loads at once.
        self._lock = threading.Lock()

    def get(self) -> Optional[M]:
        """The cached model, constructing it on first call.

        Returns None after a failed construction — the failure is cached too,
        so it costs one attempt, not one per query.
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
                self.model = self._construct()
                return self.model
            except Exception as e:  # noqa: BLE001
                self.load_failed = True
                self._on_error(e)
                return None
