"""Coverage-focused tests for the semantic-search layer: the vector store/KNN/
search-fusion (:mod:`thread_archive._retrieval.vectors`), the in-process embed
provider (:mod:`~.embed`), and the cross-encoder re-rank (:mod:`~.rerank`).

The suite is pinned model-free by conftest (embed/rerank ``is_available`` forced
False so nothing cold-loads torch). Tests that need the machinery opt back in
per-test with a FAKE model — injecting a fake ``sentence_transformers`` /
``torch`` into ``sys.modules``, or placing a scripted model in the module's
public ``SLOT`` — so no real weights load.
"""

from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import numpy as np
from sqlalchemy import text as sa_text

from thread_archive._retrieval import embed, rerank, vectors
from thread_archive._store import get_session, init_db

from .helpers import import_cc_session

# The real availability gates, captured at import (before conftest's autouse
# fixture swaps them for lambdas) so the real is_available() logic can be exercised.
_REAL_EMBED_IS_AVAILABLE = embed.is_available
_REAL_RERANK_IS_AVAILABLE = rerank.is_available


def _unit(*nonzero) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    for i, val in nonzero:
        v[i] = val
    return v


# ══════════════════════════════════════════════════════════════════════════════
# embed.py
# ══════════════════════════════════════════════════════════════════════════════
def test_embed_model_name_and_space_key() -> None:
    assert embed.model_name() == embed._MODEL_NAME
    assert embed.space_key() == "local:" + embed._MODEL_NAME


def test_embed_device_env_override(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cuda:1")
    assert embed._device() == "cuda:1"


def test_embed_device_probes_torch_accelerators(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_DEVICE", raising=False)
    fake_torch = types.ModuleType("torch")
    fake_torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True))
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert embed._device() == "mps"
    fake_torch.backends.mps.is_available = lambda: False
    fake_torch.cuda.is_available = lambda: True
    assert embed._device() == "cuda"
    fake_torch.cuda.is_available = lambda: False
    assert embed._device() == "cpu"


def test_embed_device_falls_back_to_cpu_without_torch(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_DEVICE", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` -> ImportError
    assert embed._device() == "cpu"


def test_embed_is_available_true_when_extra_present(monkeypatch) -> None:
    import importlib.util as ilu

    monkeypatch.setattr(embed, "is_available", _REAL_EMBED_IS_AVAILABLE)
    monkeypatch.setattr(ilu, "find_spec", lambda name: object())  # extra present
    monkeypatch.setattr(embed.SLOT, "load_failed", False)
    assert embed.is_available() is True


def test_embed_is_available_false_when_load_failed(monkeypatch) -> None:
    monkeypatch.setattr(embed, "is_available", _REAL_EMBED_IS_AVAILABLE)
    monkeypatch.setattr(embed.SLOT, "load_failed", True)
    assert embed.is_available() is False


def test_embed_is_available_false_without_spec(monkeypatch) -> None:
    import importlib.util as ilu

    monkeypatch.setattr(embed, "is_available", _REAL_EMBED_IS_AVAILABLE)
    monkeypatch.setattr(ilu, "find_spec", lambda name: None)
    assert embed.is_available() is False


def test_embed_is_available_false_on_import_error(monkeypatch) -> None:
    import importlib.util as ilu

    def boom(name):
        raise ImportError("nope")

    monkeypatch.setattr(embed, "is_available", _REAL_EMBED_IS_AVAILABLE)
    monkeypatch.setattr(ilu, "find_spec", boom)
    assert embed.is_available() is False


def test_hub_cache_dir_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hubA"))
    assert embed._hub_cache_dir() == str(tmp_path / "hubA")
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert embed._hub_cache_dir() == os.path.join(str(tmp_path / "hf"), "hub")
    monkeypatch.delenv("HF_HOME")
    assert embed._hub_cache_dir().endswith("/.cache/huggingface/hub")


def _seed_hub_cache(tmp_path) -> None:
    """A real, non-empty HF snapshots tree for embed's default model under
    ``tmp_path/hub`` — point HUGGINGFACE_HUB_CACHE there and ``_model_cached()``
    reads True off disk, no faking."""
    folder = "models--" + embed._MODEL_NAME.replace("/", "--")
    snaps = tmp_path / "hub" / folder / "snapshots" / "rev1"
    snaps.mkdir(parents=True)
    (snaps / "config.json").write_text("{}")


def test_model_cached_true_and_false(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    assert embed._model_cached() is False  # empty cache
    _seed_hub_cache(tmp_path)
    assert embed._model_cached() is True


def test_model_cached_swallows_oserror(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "hub"
    folder = "models--" + embed._MODEL_NAME.replace("/", "--")
    (cache / folder / "snapshots").mkdir(parents=True)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache))

    def boom(_p):
        raise OSError("scandir blew up")

    monkeypatch.setattr(embed.os, "scandir", boom)
    assert embed._model_cached() is False


def test_pin_offline_noop_when_online_forced(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_ONLINE", "1")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached()
    assert "HF_HUB_OFFLINE" not in os.environ


def test_pin_offline_noop_when_not_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))  # empty
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached()
    assert "HF_HUB_OFFLINE" not in os.environ


def test_pin_offline_sets_env_and_flips_hub_constant(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    _seed_hub_cache(tmp_path)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    fake_const = types.ModuleType("huggingface_hub.constants")
    fake_const.HF_HUB_OFFLINE = False
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", fake_const)
    embed._pin_offline_if_cached()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert fake_const.HF_HUB_OFFLINE is True


def test_pin_offline_without_hub_constants_loaded(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    _seed_hub_cache(tmp_path)
    monkeypatch.delitem(sys.modules, "huggingface_hub.constants", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached()
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_embed_load_constructs_and_caches(monkeypatch) -> None:
    made = []

    class FakeST:
        def __init__(self, name, revision=None, trust_remote_code=False, device=None):
            made.append((name, revision, trust_remote_code, device))

        def get_sentence_embedding_dimension(self):
            return 768

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cpu")
    monkeypatch.setattr(embed.SLOT, "model", None)
    monkeypatch.setattr(embed.SLOT, "load_failed", False)

    m = embed._load()
    assert m is not None
    # The default model must load its pinned snapshot (trust_remote_code means
    # a floating revision executes whatever upstream pushes).
    assert embed._MODEL_REVISION is not None
    assert made == [(embed._MODEL_NAME, embed._MODEL_REVISION, True, "cpu")]
    assert embed._load() is m  # cached — no second construction
    assert len(made) == 1


def test_embed_load_failure_is_cached(monkeypatch) -> None:
    class BadST:
        def __init__(self, *a, **k):
            raise RuntimeError("no weights on disk")

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = BadST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cpu")
    monkeypatch.setattr(embed.SLOT, "model", None)
    monkeypatch.setattr(embed.SLOT, "load_failed", False)

    assert embed._load() is None
    assert embed.SLOT.load_failed is True
    assert embed._load() is None  # cached failure short-circuits


def test_embed_load_returns_cached_model(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(embed.SLOT, "model", sentinel)
    assert embed._load() is sentinel


def test_embed_load_returns_none_when_previously_failed(monkeypatch) -> None:
    monkeypatch.setattr(embed.SLOT, "model", None)
    monkeypatch.setattr(embed.SLOT, "load_failed", True)
    assert embed._load() is None


def test_encode_returns_none_when_unavailable() -> None:
    # conftest forces is_available False -> the encode seam sits out.
    assert embed._encode(["search_query: hi"]) is None


def test_encode_returns_none_when_model_is_none(monkeypatch) -> None:
    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed.SLOT, "model", None)
    monkeypatch.setattr(embed.SLOT, "load_failed", True)
    assert embed._encode(["x"]) is None


def test_encode_success_returns_float_lists(monkeypatch) -> None:
    class FakeModel:
        def encode(self, prefixed, normalize_embeddings, convert_to_numpy):
            assert normalize_embeddings is False
            assert convert_to_numpy is True
            return np.ones((len(prefixed), 4), dtype=np.float64)

    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed.SLOT, "model", FakeModel())
    out = embed._encode(["a", "b"])
    assert out == [[1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0]]


def test_encode_swallows_model_error(monkeypatch) -> None:
    class Boom:
        def encode(self, *a, **k):
            raise RuntimeError("cuda oom")

    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed.SLOT, "model", Boom())
    assert embed._encode(["x"]) is None


def test_embed_query_prefixes_and_short_circuits(monkeypatch) -> None:
    captured = {}

    class FakeModel:
        def encode(self, prefixed, normalize_embeddings, convert_to_numpy):
            captured["p"] = prefixed
            return np.asarray([[0.5, 0.5]], dtype=np.float32)

    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed.SLOT, "model", FakeModel())
    assert embed.embed_query("  hello  ") == [0.5, 0.5]
    assert captured["p"][0].startswith("search_query: ")
    assert embed.embed_query("   ") is None
    assert embed.embed_query("") is None


def test_embed_query_none_when_encode_none() -> None:
    # conftest pins is_available False → the encode seam sits out → None.
    assert embed.embed_query("hello") is None


def test_embed_documents_prefixes_caps_and_empties(monkeypatch) -> None:
    captured = {}

    class FakeModel:
        def encode(self, prefixed, normalize_embeddings, convert_to_numpy):
            captured["p"] = prefixed
            return np.ones((len(prefixed), 1), dtype=np.float32)

    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed.SLOT, "model", FakeModel())
    long = "z" * (embed.EMBEDDING_CHAR_CAP + 50)
    out = embed.embed_documents([long, "b"])
    assert len(out) == 2
    assert captured["p"][0].startswith("search_document: ")
    assert len(captured["p"][0]) == len("search_document: ") + embed.EMBEDDING_CHAR_CAP
    assert embed.embed_documents([]) is None


# ══════════════════════════════════════════════════════════════════════════════
# rerank.py
# ══════════════════════════════════════════════════════════════════════════════
def test_rerank_model_name() -> None:
    assert rerank.model_name() == rerank._MODEL_NAME


def test_rerank_device_env_precedence(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK_DEVICE", "cuda:0")
    assert rerank._device() == "cuda:0"
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK_DEVICE")
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cpu")  # shared embed device
    assert rerank._device() == "cpu"


def test_rerank_device_probes_torch(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK_DEVICE", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_DEVICE", raising=False)
    fake_torch = types.ModuleType("torch")
    fake_torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
    fake_torch.cuda = SimpleNamespace(is_available=lambda: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert rerank._device() == "cuda"
    fake_torch.cuda.is_available = lambda: False
    assert rerank._device() == "cpu"
    fake_torch.backends.mps.is_available = lambda: True
    assert rerank._device() == "mps"


def test_rerank_device_falls_back_to_cpu_without_torch(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK_DEVICE", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_DEVICE", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)
    assert rerank._device() == "cpu"


def test_rerank_is_available_variants(monkeypatch) -> None:
    import importlib.util as ilu

    monkeypatch.setattr(rerank, "is_available", _REAL_RERANK_IS_AVAILABLE)
    monkeypatch.setattr(ilu, "find_spec", lambda n: object())  # extra present
    monkeypatch.setattr(rerank.SLOT, "load_failed", False)
    assert rerank.is_available() is True
    monkeypatch.setattr(rerank.SLOT, "load_failed", True)
    assert rerank.is_available() is False
    monkeypatch.setattr(rerank.SLOT, "load_failed", False)
    monkeypatch.setattr(ilu, "find_spec", lambda n: None)
    assert rerank.is_available() is False


def test_rerank_is_available_import_error(monkeypatch) -> None:
    import importlib.util as ilu

    def boom(n):
        raise ImportError()

    monkeypatch.setattr(rerank, "is_available", _REAL_RERANK_IS_AVAILABLE)
    monkeypatch.setattr(ilu, "find_spec", boom)
    assert rerank.is_available() is False


def test_rerank_load_cpu_no_fp16(monkeypatch) -> None:
    made = []

    class FakeCE:
        def __init__(self, name, device=None, model_kwargs=None):
            made.append((name, device, model_kwargs))

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = FakeCE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK_DEVICE", "cpu")
    monkeypatch.setattr(rerank.SLOT, "model", None)
    monkeypatch.setattr(rerank.SLOT, "load_failed", False)

    m = rerank._load()
    assert m is not None
    assert made == [(rerank._MODEL_NAME, "cpu", {})]
    assert rerank._load() is m  # cached


def test_rerank_load_accelerator_uses_fp16(monkeypatch) -> None:
    made = []

    class FakeCE:
        def __init__(self, name, device=None, model_kwargs=None):
            made.append((name, device, model_kwargs))

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = FakeCE
    fake_torch = types.ModuleType("torch")
    fake_torch.float16 = "fp16-sentinel"
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK_DEVICE", "mps")
    monkeypatch.setattr(rerank.SLOT, "model", None)
    monkeypatch.setattr(rerank.SLOT, "load_failed", False)

    assert rerank._load() is not None
    name, device, kwargs = made[0]
    assert device == "mps"
    assert kwargs == {"torch_dtype": "fp16-sentinel"}


def test_rerank_load_returns_cached_and_failed(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(rerank.SLOT, "model", sentinel)
    assert rerank._load() is sentinel
    monkeypatch.setattr(rerank.SLOT, "model", None)
    monkeypatch.setattr(rerank.SLOT, "load_failed", True)
    assert rerank._load() is None


def test_rerank_load_failure_is_cached(monkeypatch) -> None:
    class BadCE:
        def __init__(self, *a, **k):
            raise RuntimeError("no weights")

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = BadCE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK_DEVICE", "cpu")
    monkeypatch.setattr(rerank.SLOT, "model", None)
    monkeypatch.setattr(rerank.SLOT, "load_failed", False)

    assert rerank._load() is None
    assert rerank.SLOT.load_failed is True
    assert rerank._load() is None


def test_rerank_scores_success_and_pairs(monkeypatch) -> None:
    class FakeModel:
        def predict(self, pairs, batch_size, show_progress_bar):
            assert batch_size == rerank._PREDICT_BATCH_SIZE
            assert show_progress_bar is False
            self.pairs = pairs
            return np.asarray([0.2, 0.8])

    fm = FakeModel()
    monkeypatch.setattr(rerank.SLOT, "model", fm)
    out = rerank.rerank_scores("q", ["doc a", "doc b"])
    assert out == [0.2, 0.8]
    assert all(isinstance(s, float) for s in out)
    assert fm.pairs[0] == ["q", "doc a"]


def test_rerank_scores_caps_docs_and_handles_none_text(monkeypatch) -> None:
    captured = {}

    class FakeModel:
        def predict(self, pairs, batch_size, show_progress_bar):
            captured["pairs"] = pairs
            return [1.0, 2.0]

    monkeypatch.setattr(rerank.SLOT, "model", FakeModel())
    long = "y" * (rerank.RERANK_DOC_CHARS + 100)
    rerank.rerank_scores("q", [long, None])
    assert len(captured["pairs"][0][1]) == rerank.RERANK_DOC_CHARS
    assert captured["pairs"][1][1] == ""  # None doc -> ""


def test_rerank_scores_none_paths(monkeypatch) -> None:
    assert rerank.rerank_scores("", ["a"]) is None  # empty query
    assert rerank.rerank_scores("q", []) is None  # empty docs
    monkeypatch.setattr(rerank.SLOT, "model", None)
    monkeypatch.setattr(rerank.SLOT, "load_failed", True)
    assert rerank.rerank_scores("q", ["a"]) is None  # model unavailable


def test_rerank_scores_swallows_predict_error(monkeypatch) -> None:
    class Boom:
        def predict(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(rerank.SLOT, "model", Boom())
    assert rerank.rerank_scores("q", ["a"]) is None


def test_rerank_reorders_and_fail_soft(monkeypatch) -> None:
    monkeypatch.setattr(rerank, "rerank_scores", lambda q, docs: [0.1, 0.9, 0.5])
    assert rerank.rerank("q", ["A", "B", "C"], get_text=lambda x: x) == ["B", "C", "A"]
    monkeypatch.setattr(rerank, "rerank_scores", lambda q, docs: None)
    assert rerank.rerank("q", ["A"], get_text=lambda x: x) is None
    assert rerank.rerank("q", [], get_text=lambda x: x) is None


# ══════════════════════════════════════════════════════════════════════════════
# vectors.py — guards, store, KNN, sidecar, search
# ══════════════════════════════════════════════════════════════════════════════
def test_is_available_false_on_engine_error(monkeypatch) -> None:
    def boom():
        raise RuntimeError("engine down")

    monkeypatch.setattr(vectors, "get_engine", boom)
    assert vectors.is_available() is False


def test_normalize_zero_vector_returns_as_is() -> None:
    out = vectors._normalize(np.zeros(768, dtype=np.float32))
    assert not out.any()


def test_scope_content_types() -> None:
    all_embedded = list(
        vectors._USER_CONTENT_TYPES
        + vectors._ASSISTANT_CONTENT_TYPES
        + vectors._META_CONTENT_TYPES
    )
    assert vectors._scope_content_types(None) == all_embedded
    assert vectors._scope_content_types(["user", "tool"]) == ["user"]
    assert vectors._scope_content_types(["tool", "bogus"]) is None


def test_in_clause_positive_and_negative() -> None:
    p: dict = {}
    assert vectors._in_clause("t.source", ["a", "b"], "src", p, negate=False) == \
        "t.source IN (:src0,:src1)"
    assert p == {"src0": "a", "src1": "b"}
    q: dict = {}
    assert vectors._in_clause("f.content_type", ["tool"], "xct", q, negate=True) == \
        "f.content_type NOT IN (:xct0)"
    assert q == {"xct0": "tool"}


def test_unavailable_guards_short_circuit(archive_home, tmp_path, monkeypatch) -> None:
    truth = tmp_path / "truth"
    truth.mkdir()
    monkeypatch.setattr(vectors, "is_available", lambda: False)
    assert vectors.ensure_index() is False
    assert vectors.index_vectors([(1, "user", _unit((0, 1.0)))]) == 0
    assert vectors.index_events_local() == 0
    assert vectors.save_vectors_sidecar(truth) == 0
    assert vectors.load_vectors_sidecar(truth) == 0
    assert vectors.search("hi") is None
    assert vectors.get_status() == {"available": False}


def test_index_vectors_skips_wrong_dim(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    # A single wrong-dim vector is skipped; with no valid rows the upsert is a no-op.
    assert vectors.index_vectors([(1, "user", np.ones(10, dtype=np.float32))]) == 0
    assert vectors.get_status()["indexed"] == 0
    # A valid one alongside a bad one: only the valid row lands.
    assert vectors.index_vectors(
        [(2, "user", _unit((0, 1.0))), (3, "user", np.ones(5, dtype=np.float32))]
    ) == 1


def test_write_doc_vectors_skips_wrong_dim(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors._write_doc_vectors([(5, "user", [np.ones(3, dtype=np.float32)])])
    assert vectors.get_status()["indexed"] == 0


def test_index_events_local_noop_when_embed_unavailable(archive_home) -> None:
    import_cc_session(archive_home)
    # conftest keeps embed.is_available False → vector store is SQLite but the
    # embed backend sits out → 0 docs embedded.
    assert vectors.index_events_local() == 0


def test_index_events_local_stops_when_embed_returns_none(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda docs: None)
    # batch_size=1 forces a flush on the first pending doc; the None embed return
    # stops the pass mid-loop with nothing written.
    assert vectors.index_events_local(batch_size=1) == 0
    assert vectors.get_status()["indexed"] == 0


def test_knn_empty_matrix_returns_empty(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    assert vectors._knn(_unit((0, 1.0)).tolist(), ("user",), cand=5) == []


def test_knn_allowed_ids_no_match_returns_empty(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    res = vectors._knn(
        _unit((0, 1.0)).tolist(), ("user",), cand=5,
        allowed_ids=np.asarray([999], dtype=np.int64),
    )
    assert res == []


def test_knn_argpartition_when_more_live_than_cand(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([
        (1, "user", _unit((0, 1.0))),
        (2, "user", _unit((0, 0.9), (1, 0.1))),
        (3, "user", _unit((0, 0.8), (1, 0.2))),
    ])
    res = vectors._knn(_unit((0, 1.0)).tolist(), ("user",), cand=2)
    assert len(res) == 2  # cut to cand via argpartition
    assert res[0][0] == 1  # nearest survives the cut


def test_matrix_cache_recomputes_on_token_change(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors._MATRIX_CACHE.clear()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    ids1, *_ = vectors._load_matrix(("user",))
    assert len(ids1) == 1
    # An insert bumps the validity token; the cached entry for this key is stale
    # and must be recomputed in place (not evicted, key already present).
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])
    ids2, *_ = vectors._load_matrix(("user",))
    assert len(ids2) == 2


def test_get_status_reports_indexed_count(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    assert vectors.get_status() == {
        "available": True, "indexed": 1, "table": "event_vectors", "dim": 768,
    }


def test_get_status_zero_when_table_absent(archive_home) -> None:
    init_db()  # no ensure_index → event_vectors table not yet created
    assert vectors.get_status() == {
        "available": True, "indexed": 0, "table": "event_vectors", "dim": 768,
    }


# ── sidecar persistence ───────────────────────────────────────────────────────
def test_save_sidecar_noop_without_vectors(archive_home, tmp_path) -> None:
    init_db()
    truth = tmp_path / "truth"
    truth.mkdir()
    assert vectors.save_vectors_sidecar(truth) == 0  # table absent
    vectors.ensure_index()
    assert vectors.save_vectors_sidecar(truth) == 0  # table present but empty
    assert not (truth / "vectors.sqlite").exists()


def test_save_and_load_sidecar_roundtrip(archive_home, tmp_path) -> None:
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0))), (2, "text", _unit((1, 1.0)))])
    truth = tmp_path / "truth"
    truth.mkdir()
    assert vectors.save_vectors_sidecar(truth, space_key="local:test") == 2
    assert (truth / "vectors.sqlite").exists()

    with get_session() as s:
        s.execute(sa_text("DELETE FROM event_vectors"))
        s.commit()
    vectors._bump_version()
    assert vectors.get_status()["indexed"] == 0

    assert vectors.load_vectors_sidecar(truth, space_key="local:test") == 2
    assert vectors.get_status()["indexed"] == 2


def test_load_sidecar_missing_file(archive_home, tmp_path) -> None:
    init_db()
    truth = tmp_path / "truth"
    truth.mkdir()
    assert vectors.load_vectors_sidecar(truth) == 0


def test_load_sidecar_space_mismatch_reembeds(archive_home, tmp_path) -> None:
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    truth = tmp_path / "truth"
    truth.mkdir()
    vectors.save_vectors_sidecar(truth, space_key="local:OLD")
    with get_session() as s:
        s.execute(sa_text("DELETE FROM event_vectors"))
        s.commit()
    vectors._bump_version()
    assert vectors.load_vectors_sidecar(truth, space_key="local:NEW") == 0
    assert vectors.get_status()["indexed"] == 0


def test_load_sidecar_prechunk_restores_as_chunk_zero(archive_home, tmp_path) -> None:
    import sqlite3

    init_db()
    vectors.ensure_index()
    truth = tmp_path / "truth"
    truth.mkdir()
    side = truth / "vectors.sqlite"
    con = sqlite3.connect(str(side))
    con.execute(
        "CREATE TABLE event_vectors (event_id INTEGER, content_type TEXT, dim INTEGER, vec BLOB)"
    )
    con.execute(
        "INSERT INTO event_vectors VALUES (9, 'user', 768, ?)",
        (_unit((0, 1.0)).tobytes(),),
    )
    con.execute("CREATE TABLE vector_meta (space_key TEXT)")
    con.execute("INSERT INTO vector_meta VALUES ('local:PC')")
    con.commit()
    con.close()

    assert vectors.load_vectors_sidecar(truth, space_key="local:PC") == 1
    with get_session() as s:
        row = s.execute(
            sa_text("SELECT event_id, content_type, chunk FROM event_vectors")
        ).one()
    assert tuple(row) == (9, "user", 0)


# ── search: the full embed→KNN→hydrate path (query embed faked) ───────────────
def _index_user_and_text(qvec) -> None:
    """Index one vector per embeddable events_fts row using ``qvec``."""
    with get_session() as s:
        rows = s.execute(sa_text(
            "SELECT event_id, content_type FROM events_fts "
            "WHERE content_type IN ('user', 'text') AND content != ''"
        )).all()
    vectors.index_vectors([(int(eid), ct, qvec) for eid, ct in rows])


def test_search_unscoped_hydrates_hits(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    hits = vectors.search("hello")
    assert hits is not None and len(hits) >= 1
    assert all("_semantic" in h for h in hits)
    assert hits[0]["_semantic"] == 1.0  # identical vector → cosine 1


def test_search_scoped_by_thread(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    with get_session() as s:
        tid = int(s.execute(sa_text("SELECT id FROM threads LIMIT 1")).scalar())
    hits = vectors.search("hello", thread_id=tid)
    assert hits and all(h["thread_id"] == tid for h in hits)


def test_search_scoped_thread_without_events_is_empty(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    assert vectors.search("hello", thread_id=999_999) == []


def test_search_source_and_time_window(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    with get_session() as s:
        src = s.execute(sa_text("SELECT source FROM threads LIMIT 1")).scalar()
    hits = vectors.search(
        "hello", source=[src],
        since="2020-01-01T00:00:00Z", until="2030-01-01T00:00:00Z",
    )
    assert hits and len(hits) >= 1
    # An unknown source scopes to no events → empty (not None).
    assert vectors.search("hello", source=["no-such-source"]) == []


def test_search_partial_exclude_drops_type_in_hydration(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    hits = vectors.search("hello", exclude_content_types=["text"])
    assert hits is not None
    assert all(h["content_type"] != "text" for h in hits)


def test_search_exclude_all_embedded_returns_none(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    assert vectors.search(
        "hi", exclude_content_types=["user", "text", "title", "summary"]
    ) is None


def test_search_empty_scope_when_no_candidates(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)  # only user/text vectors exist
    # Scope to 'title' (no vectors) → KNN finds nothing → [].
    assert vectors.search("hello", content_types=["title"]) == []


def test_search_none_on_empty_query(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    assert vectors.search("") is None
    assert vectors.search("   ") is None


def test_search_none_when_nothing_indexed(archive_home, monkeypatch) -> None:
    init_db()
    vectors.ensure_index()
    # Guard the assertion: even if reached, embed must not be consulted here.
    monkeypatch.setattr(
        embed, "embed_query",
        lambda text: (_ for _ in ()).throw(AssertionError("should not embed")),
    )
    assert vectors.search("anything") is None


def test_search_none_when_query_embed_returns_none(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    _index_user_and_text(q)
    monkeypatch.setattr(embed, "embed_query", lambda text: None)
    assert vectors.search("hello") is None


def test_search_none_when_query_embed_raises(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    _index_user_and_text(q)

    def boom(text):
        raise RuntimeError("embed exploded")

    monkeypatch.setattr(embed, "embed_query", boom)
    assert vectors.search("hello") is None


# ── the successful in-process embed drain (embed backend faked) ───────────────
def test_search_hydration_skips_row_without_candidate_sim(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    monkeypatch.setattr(embed, "embed_query", lambda text: q.tolist())
    _index_user_and_text(q)
    # Inject an extra events_fts row for the user event under a content_type that
    # was never embedded (no vector → never a KNN candidate). Hydration selects by
    # event_id, not content_type, so the row surfaces but has no candidate
    # similarity and is skipped.
    with get_session() as s:
        uid = int(s.execute(sa_text(
            "SELECT event_id FROM events_fts WHERE content_type='user' LIMIT 1"
        )).scalar())
        tid = int(s.execute(sa_text(
            "SELECT thread_id FROM events_fts WHERE event_id=:e LIMIT 1"), {"e": uid}
        ).scalar())
        s.execute(sa_text(
            "INSERT INTO events_fts "
            "(event_id, thread_id, event_type, content, content_type, tool_name) "
            "VALUES (:event_id, :thread_id, :event_type, :content, :content_type, :tool_name)"
        ), {"event_id": uid, "thread_id": tid, "event_type": "thread_meta",
            "content": "injected meta row", "content_type": "summary", "tool_name": None})
        s.commit()
    hits = vectors.search("hello")
    assert hits is not None
    assert all(h["content_type"] != "summary" for h in hits)  # injected row skipped


def test_index_events_local_full_drain(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)  # 1 user + 1 assistant-text = 2 embeddable docs
    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda docs: [_unit((0, 1.0)) for _ in docs])
    assert vectors.index_events_local() == 2  # single end-of-loop flush drains both
    assert vectors.get_status()["indexed"] == 2
    assert vectors.index_events_local() == 0  # caught up → final flush no-op


def test_index_events_local_capped_newest_first(archive_home, monkeypatch) -> None:
    import_cc_session(archive_home)
    monkeypatch.setattr(embed, "is_available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda docs: [_unit((0, 1.0)) for _ in docs])
    assert vectors.index_events_local(max_events=1, newest_first=True) == 1
    assert vectors.get_status()["indexed"] == 1


def test_ensure_index_migrates_prechunk_table(archive_home) -> None:
    init_db()
    with get_session() as s:
        s.execute(sa_text(
            "CREATE TABLE event_vectors (event_id INTEGER NOT NULL, content_type TEXT NOT NULL, "
            "dim INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (event_id, content_type))"
        ))
        s.execute(sa_text(
            "INSERT INTO event_vectors (event_id, content_type, dim, vec) VALUES (7, 'user', 768, :v)"
        ), {"v": _unit((0, 1.0)).tobytes()})
        s.commit()

    assert vectors.ensure_index() is True
    with get_session() as s:
        rows = s.execute(
            sa_text("SELECT event_id, content_type, chunk FROM event_vectors")
        ).all()
    assert [tuple(r) for r in rows] == [(7, "user", 0)]  # carried over as chunk 0


def test_load_matrix_returns_cached_on_hit(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    vectors._MATRIX_CACHE.clear()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    first = vectors._load_matrix(("user",))
    second = vectors._load_matrix(("user",))  # unchanged token → cache hit
    assert second[0] is first[0]  # same cached ndarray object returned


def test_embed_and_rerank_warm(monkeypatch) -> None:
    for mod in (embed, rerank):
        monkeypatch.setattr(mod, "is_available", lambda: False)
        assert mod.warm() is False  # unavailable → loader untouched
    for mod in (embed, rerank):
        monkeypatch.setattr(mod, "is_available", lambda: True)
        monkeypatch.setattr(mod.SLOT, "model", object())     # already loaded
        assert mod.warm() is True
        monkeypatch.setattr(mod.SLOT, "model", None)
        monkeypatch.setattr(mod.SLOT, "load_failed", True)   # cached failure
        assert mod.warm() is False
