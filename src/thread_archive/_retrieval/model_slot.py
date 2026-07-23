"""One lazily-constructed heavy model + its cached-failure flag.

Shared by :mod:`.embed` and :mod:`.rerank`, whose torch models are far too heavy
to construct twice. The slot takes its loader at construction, so it holds any
model, and accepts an already-loaded one — a caller that already has a
SentenceTransformer hands it in rather than paying for a second copy.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Generic, Optional, TypeVar

M = TypeVar("M")

# Process policy: when set, the request path serves without a model it would have
# to CONSTRUCT — it uses a model only once already loaded, leaving the
# tens-of-seconds construction to an explicit :meth:`ModelSlot.get` (an
# on-startup ``warm()``), never a query. A long-running server sets this so a
# query arriving before warming finishes returns fast (lexical-only) instead of
# blocking on the cold load; a one-shot CLI leaves it off and loads lazily. Read
# through :func:`defer_construction` so the request-path guards in ``embed`` /
# ``rerank`` honor a value set after import.
_DEFER_CONSTRUCTION = False


def set_defer_construction(on: bool) -> None:
    """Set the process load policy (see :data:`_DEFER_CONSTRUCTION`)."""
    global _DEFER_CONSTRUCTION
    _DEFER_CONSTRUCTION = on


def defer_construction() -> bool:
    """Whether the request path must not construct a model (warm loads it instead)."""
    return _DEFER_CONSTRUCTION


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
        # Serializes *use* of the one shared model. A torch forward pass isn't
        # reentrant — two threads driving the same model at once corrupt its
        # internal length-sized buffers (a wrong-shape tensor from the other
        # call's sequence length). One model per process is the whole point of
        # the slot, so guarding concurrent use is the slot's job too.
        self._use_lock = threading.Lock()

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

    @contextmanager
    def use(self) -> Iterator[Optional[M]]:
        """The loaded model, yielded while holding the use-lock so the caller's
        forward pass can't overlap another thread's on the same shared model.
        Yields ``None`` when the model is unavailable (and holds no lock then) —
        the caller degrades exactly as it does for a failed :meth:`get`. Keep only
        the model call inside the ``with``; do post-processing after it to hold the
        lock no longer than the forward pass needs."""
        model = self.get()
        if model is None:
            yield None
            return
        with self._use_lock:
            yield model
