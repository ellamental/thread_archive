"""The open archive as an object: what it owns, and what closing it takes with it.

These pin the properties an archive-as-object provides and a module global per
cache cannot: caches that belong to one archive and die with it, identity that is
not an address, and two archives open at once.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text

from thread_archive._retrieval import embed_graph, fts, vectors
from thread_archive._store import (
    Archive,
    build_engine,
    close_engine,
    current_archive,
    current_archive_or_none,
    get_session,
    init_db,
    init_engine,
    use_engine,
)


def _unit(*pairs, dim: int = 768) -> list[float]:
    vec = [0.0] * dim
    for i, v in pairs:
        vec[i] = v
    return vec


# ── identity ─────────────────────────────────────────────────────────────────

def test_tokens_are_never_reused(archive_home) -> None:
    """Identity that survives a dispose.

    CPython is free to hand a freshly allocated engine the address a disposed one
    had, so an address cannot say which archive a cached entry belongs to. Tokens
    come from a counter, so a closed archive is never confused with an open one."""
    init_db()
    seen = []
    for _ in range(5):
        seen.append(current_archive().token)
        close_engine()
    assert len(set(seen)) == 5, f"tokens repeated across closes: {seen}"
    assert seen == sorted(seen), "tokens should be drawn in open order"


def test_closing_leaves_no_archive_open(archive_home) -> None:
    init_db()
    assert current_archive_or_none() is not None
    close_engine()
    assert current_archive_or_none() is None


def test_reopening_the_same_home_keeps_one_archive(archive_home) -> None:
    """Re-opening the home already open is not a new archive — the caches an
    engine-rebuild would have thrown away are exactly what a serving process
    depends on keeping."""
    init_db()
    first = current_archive().token
    init_engine()  # same DSN
    assert current_archive().token == first


# ── caches belong to the archive ─────────────────────────────────────────────

def test_closing_an_archive_drops_its_caches(archive_home) -> None:
    """One call is the whole teardown — what the suite's isolation fixture rests
    on, and why a new cache never needs a line added to it."""
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    vectors._load_matrix(("user",))
    arch = current_archive()
    assert arch.cache(vectors._MATRIX_SLOT), "a built matrix should be cached"

    close_engine()
    init_db()
    fresh = current_archive()
    assert fresh is not arch
    assert not fresh.cache(vectors._MATRIX_SLOT), "a new archive starts with no matrix"


def test_a_second_archive_caches_separately(archive_home, tmp_path) -> None:
    """Two archives open at once, each with its own caches.

    ``use_engine`` opens a transient archive around another index file for the
    block. Anything it caches is that archive's, and the outer archive's cache is
    untouched by it and still its own afterwards."""
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    vectors._load_matrix(("user",))
    outer = current_archive()
    outer_entry = outer.cache(vectors._MATRIX_SLOT)[vectors._matrix_key(("user",))]

    other = tmp_path / "other.db"
    engine = build_engine(f"sqlite:///{other}")
    try:
        with use_engine(engine):
            inner = current_archive()
            assert inner is not outer
            assert inner.token != outer.token
            assert not inner.cache(vectors._MATRIX_SLOT), (
                "the second archive must not see the first's matrix"
            )
            inner.cache(vectors._MATRIX_SLOT)["sentinel"] = object()
    finally:
        engine.dispose()

    assert current_archive() is outer, "the override must be restored"
    assert "sentinel" not in outer.cache(vectors._MATRIX_SLOT), (
        "the inner archive's cache must not have leaked outward"
    )
    assert outer.cache(vectors._MATRIX_SLOT)[vectors._matrix_key(("user",))] is outer_entry


def test_cache_slots_do_not_collide(archive_home) -> None:
    """Each layer namespaces its own slot; the store knows nothing about them."""
    init_db()
    arch = current_archive()
    for slot in (vectors._MATRIX_SLOT, vectors._CHECKED_SLOT, embed_graph._SLOT, fts._MEMO_SLOT):
        arch.cache(slot)["k"] = slot
    assert {arch.cache(s)["k"] for s in
            (vectors._MATRIX_SLOT, vectors._CHECKED_SLOT, embed_graph._SLOT, fts._MEMO_SLOT)} == {
        vectors._MATRIX_SLOT, vectors._CHECKED_SLOT, embed_graph._SLOT, fts._MEMO_SLOT
    }


def test_drop_caches_keeps_the_engine(archive_home) -> None:
    """A reindex publishes a new index file under a live archive: what is derived
    from the old rows is wrong, but the archive stays open."""
    init_db()
    arch = current_archive()
    arch.cache("probe")["x"] = 1
    arch.drop_caches()
    assert not arch.cache("probe")
    with get_session() as s:
        assert s.execute(sa_text("SELECT 1")).scalar() == 1


# ── the trap ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "reset",
    [vectors.reset_matrix_cache, embed_graph.reset_cache, fts.reset_set_memo],
    ids=lambda f: f.__module__.rsplit(".", 1)[-1],
)
def test_a_reset_never_opens_an_archive(reset, monkeypatch, tmp_path) -> None:
    """Dropping a cache must not be what fixes the home.

    Opening resolves ``$THREAD_ARCHIVE_HOME`` and pins it for the process. A reset
    that opens an archive to find a cache to clear therefore *chooses a home* as a
    side effect of doing nothing — and a later, deliberate choice then silently has
    no effect, because one is already open. The caller that suffers it is any setup
    step that clears caches before naming the home it wants, which is the ordinary
    shape of a fixture: it ends up pointed at whatever the environment said first."""
    close_engine()
    monkeypatch.setenv("THREAD_ARCHIVE_HOME", str(tmp_path / "never_opened"))

    reset()

    assert current_archive_or_none() is None, f"{reset.__name__} opened an archive"
    assert not (tmp_path / "never_opened").exists(), (
        f"{reset.__name__} created the home on disk"
    )


def test_a_reset_with_an_archive_open_still_clears(archive_home) -> None:
    """The no-op-when-closed guard must not turn the reset itself into a no-op."""
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    vectors._load_matrix(("user",))
    assert current_archive().cache(vectors._MATRIX_SLOT)

    vectors.reset_matrix_cache()
    assert not current_archive().cache(vectors._MATRIX_SLOT)


# ── the Archive object itself ────────────────────────────────────────────────

def test_dsn_is_derived_lazily(tmp_path) -> None:
    """An archive opened around a caller's engine is identified by the object, so it
    must not demand a URL at construction — an engine stub that never gets matched
    on a DSN is a legitimate thing to run the query path against."""

    class _NoUrl:
        def dispose(self) -> None:
            pass

    arch = Archive(_NoUrl())  # constructs fine
    assert arch.token > 0
    with pytest.raises(AttributeError):
        _ = arch.dsn


def test_close_is_idempotent(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'x.db'}")
    arch = Archive(engine)
    arch.cache("probe")["x"] = 1
    arch.close()
    arch.close()
    assert not arch.cache("probe")
