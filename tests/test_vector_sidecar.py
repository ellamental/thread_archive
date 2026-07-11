"""The vector sidecar (``truth/vectors.sqlite``): the semantic index survives swaps.

* A plain ``reindex`` (no ``--vectors``) restores the sidecar into the new
  index and prunes rows for events the rebuild collapsed away.
* ``save_vectors_sidecar`` never replaces a populated sidecar with an empty
  table.
* ``backup`` refreshes the sidecar so live-embedded vectors ride the mirror.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import text

import thread_archive as ta
from thread_archive._store import get_session

from .helpers import import_cc_session


def _event_ids() -> list[int]:
    with get_session() as s:
        return [r[0] for r in s.execute(text("SELECT id FROM events ORDER BY id"))]


def _index_fake_vectors(event_ids: list[int]) -> None:
    from thread_archive._retrieval.vectors import index_vectors

    assert index_vectors(
        (eid, "user", [float(eid % 7 + 1)] * 768) for eid in event_ids
    ) == len(event_ids)


def _vector_count() -> int:
    with get_session() as s:
        return s.execute(text("SELECT count(*) FROM event_vectors")).scalar() or 0


def test_plain_reindex_restores_sidecar_and_prunes_orphans(archive_home, tmp_path) -> None:
    from thread_archive._retrieval.vectors import save_vectors_sidecar

    import_cc_session(tmp_path)
    ids = _event_ids()
    _index_fake_vectors(ids)
    # A vector for an event the rebuild won't hold must be pruned, not scored.
    _index_fake_vectors([999_999])
    assert save_vectors_sidecar(archive_home / "truth") == len(ids) + 1

    counts = ta.reindex()  # NO vectors flag — restore must happen anyway
    assert counts["vectors_restored"] == len(ids) + 1
    assert counts["vectors_pruned"] == 1
    assert _vector_count() == len(ids)


def test_empty_save_never_clobbers_a_populated_sidecar(archive_home, tmp_path) -> None:
    from thread_archive._retrieval.vectors import save_vectors_sidecar

    import_cc_session(tmp_path)
    _index_fake_vectors(_event_ids())
    n = save_vectors_sidecar(archive_home / "truth")
    assert n > 0

    with get_session() as s:
        s.execute(text("DELETE FROM event_vectors"))
        s.commit()
    assert save_vectors_sidecar(archive_home / "truth") == 0  # no-op, not an empty save

    side = sqlite3.connect(archive_home / "truth" / "vectors.sqlite")
    try:
        assert side.execute("SELECT count(*) FROM event_vectors").fetchone()[0] == n
    finally:
        side.close()


def test_backup_refreshes_the_vector_sidecar(archive_home, tmp_path) -> None:
    import_cc_session(tmp_path)
    ids = _event_ids()
    _index_fake_vectors(ids)

    res = ta.backup(str(tmp_path / "dest"))
    assert res["vectors_cached"] == len(ids)
    assert (archive_home / "truth" / "vectors.sqlite").exists()
    assert (tmp_path / "dest" / "vectors.sqlite").exists()  # rides the mirror
