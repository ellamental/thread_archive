"""Opening and closing the process's archive.

The two calls every surface makes before it does anything else, and the layer they
belong to. Opening an archive reaches exactly three things — the resolved paths
(:mod:`._config`), the SQLite store (:mod:`._store`), and the truth log's cached
append handles (:mod:`._truth`) — so it sits with the corpus, below everything
that merely *wants* an archive open. The ops kit, the watcher and the composition
layer all call it, and none of them should have to reach up to do so.

One archive per process: :func:`open_archive` pins ``$THREAD_ARCHIVE_HOME`` so the
engine (index.db), the truth log (truth/), and search all resolve to the same home.
To switch archives, call :func:`close` first.
"""

from __future__ import annotations

import os
from typing import Optional

from ._config import ENV_HOME, ArchivePaths, resolve_paths


def open_archive(home: Optional[str] = None) -> ArchivePaths:
    """Open (and initialize) the archive at ``home`` (env / default if None).

    Switching homes within a process repoints everything atomically: the engine is
    rebuilt (``init_engine``) and the JSONL truth-log's cached append handles are
    dropped, so a second ``open_archive`` to a different home can't leave SQLite
    writing one store while JSONL appends resolve to another.
    """
    paths = resolve_paths(home).ensure()
    target = paths.sqlalchemy_url
    from ._store import active_dsn, init_db, init_engine, reconnect_if_swapped

    if active_dsn() is not None and active_dsn() != target:
        from ._truth import reset_handles

        reset_handles()  # stale handles point at the previous home's files
    # Pin the home so engine + truth + search resolve consistently for the process.
    os.environ[ENV_HOME] = str(paths.home)
    try:
        init_engine(target)  # rebuilds when the DSN changed
        # Read-path convergence: long-lived processes (an MCP server, the web
        # app) pass through here on every call, so a reindex's index.db swap is
        # picked up on the next call. Writers get the authoritative check on
        # ingest-lock *acquire* (see _truth.shared_ingest_lock) — this one runs
        # before any blocking wait and can go stale during it.
        reconnect_if_swapped()
        init_db()
    except Exception:
        # A failed first connection (corrupt index, permissions, failed PRAGMA)
        # must not leave its partially initialized pool pinned globally. Apart
        # from leaking the DB-API connection, a later call would keep retrying
        # through the poisoned engine instead of starting from a clean binding.
        from ._store import close_engine

        close_engine()
        raise
    return paths


def close() -> None:
    """Close the archive so a different one can be opened.

    The engine and every cache in it go together (``_store.close_engine``); the
    truth log's append handles are process-scoped and dropped alongside.
    """
    from ._store import close_engine
    from ._truth import reset_handles

    reset_handles()
    close_engine()
