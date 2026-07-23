"""The search stage-timing probe — opt-in context-local, fail-soft."""

from __future__ import annotations

from thread_archive._retrieval import _probe


def test_no_probe_installed_is_none() -> None:
    # The guard every timing point checks: nobody listening → nothing to record.
    assert _probe.current() is None


def test_install_yields_a_fresh_probe_and_restores_on_exit() -> None:
    assert _probe.current() is None
    with _probe.install() as probe:
        assert _probe.current() is probe
        probe.fts_ms += 5.0
        probe.did_rerank = True
    # Slot restored — the probe does not leak past its block.
    assert _probe.current() is None


def test_install_nests_without_leaking() -> None:
    with _probe.install() as outer:
        with _probe.install() as inner:
            assert _probe.current() is inner
        # Leaving the inner block restores the outer probe, not None.
        assert _probe.current() is outer


def test_as_record_shape_and_cold_only_when_true() -> None:
    probe = _probe.SearchProbe()
    probe.fts_ms = 12.34
    probe.semantic_ms = 5.0
    probe.rerank_ms = 100.06
    probe.did_rerank = True
    probe.pool_size = 198
    rec = probe.as_record()
    assert rec == {"fts_ms": 12.3, "semantic_ms": 5.0, "rerank_ms": 100.1,
                   "did_rerank": True, "pool_size": 198}
    # cold is the exception (the cold-model tail), so it rides along only when set.
    assert "cold" not in rec
    probe.cold = True
    assert probe.as_record()["cold"] is True
