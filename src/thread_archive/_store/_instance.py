"""One open archive, as an object.

An archive is a home directory, the SQLite index bound to it, and whatever the
layers above have computed and want to keep — the vector matrix, the corpus
graph, an exact-set memo, an error tally. Those derived things are all *of* one
archive: they are wrong if served for another, and worthless once it closes.

This class is what owns them together. :class:`Archive` holds the engine and a
namespaced scratch dict per layer (:meth:`cache`), and :meth:`close` drops the
whole set with the engine in one step. A layer caches into its own slot and never
has to arrange for its own teardown.

**Why an object rather than a module global per cache.** Scoping an entry to the
archive that produced it needs an identity that outlives nothing — and an engine's
address is not one, because CPython recycles the id of a disposed engine, so a
cache keyed that way can hand a freshly opened archive the previous one's matrix,
graph, or set memo on a collision nobody controls. :attr:`token` is drawn from a
counter that never repeats, so identity here is real identity; and because a
cache lives *inside* the instance, the stale-read window is closed for a stronger
reason than a unique key: there is no surviving dict for a closed archive's
entries to sit in.

**Scoping is by instance, not by engine.** ``use_engine`` (the seam a reindex's
loader and the federated read path use to point the query path at a different
index file) builds a transient ``Archive`` around that engine, so a block running
against another store caches into its own slots and drops them on exit. Two
archives open in one process — the thing the module globals made impossible —
keep separate everything.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Optional

from sqlalchemy.engine import Engine

#: Instance tokens, drawn in open order and never reused — see the class docstring
#: for why ``id()`` could not be used for this.
_tokens = itertools.count(1)


class Archive:
    """One open archive: its engine, and the per-archive scratch above it."""

    __slots__ = ("token", "engine", "_dsn", "_caches", "_lock", "index_ident")

    def __init__(self, engine: Engine, dsn: Optional[str] = None) -> None:
        self.token: int = next(_tokens)
        self.engine = engine
        self._dsn = dsn
        self._caches: dict[str, Any] = {}
        self._lock = threading.Lock()
        #: ``(dsn, st_dev, st_ino)`` of the index file at the last swap check —
        #: per-instance because it describes *this* archive's file. See
        #: :func:`.._base.reconnect_if_swapped`.
        self.index_ident: Optional[tuple[str, int, int]] = None

    @property
    def dsn(self) -> str:
        """The DSN this archive is bound to, read off the engine if not given.

        Derived lazily: an archive opened around a caller's engine (``use_engine``)
        is identified by the object, never by a DSN, and asking one for a URL it
        was never going to be matched on is how a perfectly good engine stub gets
        rejected at construction.
        """
        if self._dsn is None:
            self._dsn = str(self.engine.url)
        return self._dsn

    def cache(self, name: str) -> dict:
        """This archive's scratch dict for ``name``, created on first use.

        ``name`` namespaces one layer's cache from another's; the contents are
        opaque here. The store is the lowest layer and does not know what a vector
        matrix or a corpus graph is — it knows only that they belong to an archive
        and die with it.
        """
        cached = self._caches.get(name)
        if cached is None:
            with self._lock:  # a background refresh may be racing the request thread
                cached = self._caches.setdefault(name, {})
        return cached

    def drop_caches(self) -> None:
        """Drop every layer's derived state, keeping the engine open.

        For when the underlying store moved beneath a live archive — a reindex
        published a new index file over this one — and everything derived from the
        old rows is not merely dated but wrong.
        """
        with self._lock:
            self._caches.clear()

    def close(self) -> None:
        """Dispose the engine and drop every cache. Idempotent."""
        self.drop_caches()
        self.engine.dispose()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics
        return f"<Archive token={self.token} dsn={self.dsn!r} caches={sorted(self._caches)}>"
