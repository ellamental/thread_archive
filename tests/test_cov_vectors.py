"""Coverage-focused tests for the semantic-search layer: the vector store/KNN/
search-fusion (:mod:`thread_archive._retrieval.vectors`) and the in-process embed
provider (:mod:`~.embed`).

The suite is pinned model-free by conftest (``$THREAD_ARCHIVE_EMBED`` set to
``off``), so nothing here can cold-load torch. Tests that need the machinery build
their own :class:`~.embed.Embedder` around a scripted model, or hand one to the
``embedder`` argument of the call under test — real product code over a stand-in
model, never real weights.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import text as sa_text

from thread_archive._retrieval import embed, vectors
from thread_archive._retrieval.model_slot import ModelSlot
from thread_archive._store import current_archive, get_session, init_db
from thread_archive._store._base import use_engine

from .helpers import import_cc_session


def _unit(*nonzero) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    for i, val in nonzero:
        v[i] = val
    return v


def _models_on(monkeypatch) -> None:
    """Clear conftest's model-free pin, for a test that drives the real model
    machinery over a scripted stand-in (never real weights)."""
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    monkeypatch.delenv("THREAD_ARCHIVE_RERANK", raising=False)


def _boom() -> object:
    raise RuntimeError("no weights on disk")


class _ScriptedModel:
    """A stand-in for a loaded SentenceTransformer: records what it was asked to
    encode, and returns fixed-width vectors."""

    def __init__(self, width: int = 4, dtype=np.float64) -> None:
        self.width, self.dtype, self.seen = width, dtype, []

    def encode(self, prefixed, normalize_embeddings, convert_to_numpy, show_progress_bar):
        # The contract embed relies on: un-normalized (the store normalizes on
        # write) and numpy out (it casts to float32 lists). No progress bar —
        # every caller is a daemon or a library call, and the bar would render
        # into a log file.
        assert normalize_embeddings is False
        assert convert_to_numpy is True
        assert show_progress_bar is False
        self.seen.append(list(prefixed))
        return np.ones((len(prefixed), self.width), dtype=self.dtype)


class _FixedEmbedder:
    """An embedder that answers every query with one fixed vector — the front-door
    stand-in for the torch model, so the real KNN/hydration path runs on real
    vectors without loading weights."""

    def __init__(self, vec) -> None:
        self.vec = list(vec)
        self.queries: list[str] = []

    def is_available(self) -> bool:
        return True

    def space_key(self) -> str:
        return "local:test"

    def embed_query(self, text):
        self.queries.append(text)
        return list(self.vec)

    def embed_documents(self, texts):
        return [list(self.vec) for _ in texts]


# ══════════════════════════════════════════════════════════════════════════════
# embed.py
# ══════════════════════════════════════════════════════════════════════════════
def test_embed_model_name_and_space_key_follow_the_env(monkeypatch) -> None:
    assert embed.model_name() == embed.DEFAULT_MODEL
    assert embed.space_key() == "local:" + embed.DEFAULT_MODEL
    # Read per call, not frozen at import — an operator who sets the model gets it.
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_MODEL", "acme/tiny")
    assert embed.model_name() == "acme/tiny"
    assert embed.space_key() == "local:acme/tiny"
    assert embed.Embedder().space_key() == "local:acme/tiny"
    # …while an embedder built around a named model keeps its own space.
    assert embed.Embedder("other/model").space_key() == "local:other/model"


def test_revision_pin_is_per_model_and_env_overridable(monkeypatch) -> None:
    assert embed.revision_for(embed.DEFAULT_MODEL) == embed.PINNED_REVISIONS[embed.DEFAULT_MODEL]
    assert embed.revision_for("acme/unpinned") is None  # a custom model floats…
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_REVISION", "deadbeef")
    assert embed.revision_for("acme/unpinned") == "deadbeef"  # …unless pinned explicitly
    assert embed.revision_for(embed.DEFAULT_MODEL) == "deadbeef"


def test_models_enabled_reads_its_switch_per_call(monkeypatch) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED", raising=False)
    assert embed.models_enabled() is True
    for off in ("off", "0", "false", "no", "OFF", "  off  "):
        monkeypatch.setenv("THREAD_ARCHIVE_EMBED", off)
        assert embed.models_enabled() is False, off
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "on")
    assert embed.models_enabled() is True
    # The re-rank stage reads its own switch, with the same spellings.
    monkeypatch.setenv("THREAD_ARCHIVE_RERANK", "off")
    assert embed.models_enabled("THREAD_ARCHIVE_RERANK") is False


def test_the_model_free_pin_stands_the_arm_down() -> None:
    """conftest's pin is what keeps the whole suite off real torch weights — if it
    ever stops biting, every search-path test starts cold-loading models. Every
    module-level entry point must honor it, not just the availability probe: these
    are the calls that would otherwise reach a real loader."""
    assert embed.is_available() is False
    assert embed.warm() is False
    assert embed.embed_query("anything") is None
    assert embed.embed_documents(["doc"]) is None


def test_importable_probes_without_importing() -> None:
    assert embed.importable("thread_archive") is True
    assert embed.importable("no_such_package_xyz") is False
    # A missing parent makes find_spec raise rather than answer.
    assert embed.importable("no_such_package_xyz.child") is False
    assert "no_such_package_xyz" not in sys.modules  # probed, never imported


# ── device selection ──────────────────────────────────────────────────────────
def test_select_device_prefers_override_then_accelerator() -> None:
    assert embed.select_device("cuda:1", True, True) == "cuda:1"
    assert embed.select_device(None, True, True) == "mps"
    assert embed.select_device(None, False, True) == "cuda"
    assert embed.select_device(None, False, False) == "cpu"


def test_torch_accelerator_probe_answers_on_any_box() -> None:
    """The one part that touches torch: it must answer a plain (mps, cuda) pair
    whether or not the [embeddings] extra is installed, never raise."""
    mps, cuda = embed.torch_accelerators()
    assert isinstance(mps, bool) and isinstance(cuda, bool)


def test_embed_device_override_else_the_real_probe(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cuda:1")
    assert embed._device() == "cuda:1"
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_DEVICE")
    assert embed._device() == embed.select_device(None, *embed.torch_accelerators())


# ── hub cache / offline pinning ───────────────────────────────────────────────
def test_hub_cache_dir_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hubA"))
    assert embed._hub_cache_dir() == str(tmp_path / "hubA")
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert embed._hub_cache_dir() == os.path.join(str(tmp_path / "hf"), "hub")
    monkeypatch.delenv("HF_HOME")
    assert embed._hub_cache_dir().endswith("/.cache/huggingface/hub")


def _seed_hub_cache(tmp_path, name: str | None = None) -> None:
    """A real, non-empty HF snapshots tree under ``tmp_path/hub`` — point
    HUGGINGFACE_HUB_CACHE there and ``_model_cached()`` reads True off disk."""
    folder = "models--" + (name or embed.DEFAULT_MODEL).replace("/", "--")
    snaps = tmp_path / "hub" / folder / "snapshots" / "rev1"
    snaps.mkdir(parents=True)
    (snaps / "config.json").write_text("{}")


def test_model_cached_true_and_false(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    assert embed._model_cached(embed.DEFAULT_MODEL) is False  # empty cache
    _seed_hub_cache(tmp_path)
    assert embed._model_cached(embed.DEFAULT_MODEL) is True
    # …and it's per model: a different one isn't cached just because this one is.
    assert embed._model_cached("acme/other") is False


def test_model_cached_survives_an_unreadable_cache(monkeypatch, tmp_path) -> None:
    """A hub cache the process can't read degrades to "not cached" rather than
    taking the load down with an OSError."""
    _seed_hub_cache(tmp_path)
    snaps = tmp_path / "hub" / ("models--" + embed.DEFAULT_MODEL.replace("/", "--")) / "snapshots"
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    assert embed._model_cached(embed.DEFAULT_MODEL) is True  # readable: cached
    snaps.chmod(0o000)
    try:
        if os.access(snaps, os.R_OK):  # running as root — permissions don't bite
            pytest.skip("cannot make a directory unreadable as this user")
        assert embed._model_cached(embed.DEFAULT_MODEL) is False
    finally:
        snaps.chmod(0o755)


def test_pin_offline_noop_when_online_forced(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_ONLINE", "1")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached(embed.DEFAULT_MODEL)
    assert "HF_HUB_OFFLINE" not in os.environ


def test_pin_offline_noop_when_not_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))  # empty
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached(embed.DEFAULT_MODEL)
    assert "HF_HUB_OFFLINE" not in os.environ


def test_pin_offline_sets_the_env_when_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    _seed_hub_cache(tmp_path)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    embed._pin_offline_if_cached(embed.DEFAULT_MODEL)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_pin_offline_flips_the_live_hub_constant(monkeypatch, tmp_path) -> None:
    """huggingface_hub freezes HF_HUB_OFFLINE into a module constant at import, so
    setting the env isn't enough once it's loaded — the live constant must flip too.
    Driven against the real module, so a renamed constant can't pass."""
    constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    _seed_hub_cache(tmp_path)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    before = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = False
    try:
        embed._pin_offline_if_cached(embed.DEFAULT_MODEL)
        assert constants.HF_HUB_OFFLINE is True
    finally:
        constants.HF_HUB_OFFLINE = before


def test_pin_offline_without_hub_constants_loaded(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THREAD_ARCHIVE_EMBED_ONLINE", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub"))
    _seed_hub_cache(tmp_path, "acme/never-imported")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed._pin_offline_if_cached("acme/never-imported")
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


# ── the lazy slot ─────────────────────────────────────────────────────────────
def test_model_slot_constructs_once_and_caches() -> None:
    made: list[int] = []

    def construct() -> object:
        made.append(1)
        return "MODEL"

    slot = ModelSlot(construct)
    assert slot.get() == "MODEL"
    assert slot.get() == "MODEL"
    assert made == [1]  # one construction, not one per call


def test_model_slot_caches_the_failure_and_reports_it_once() -> None:
    seen: list[Exception] = []
    attempts: list[int] = []

    def construct() -> object:
        attempts.append(1)
        raise RuntimeError("no weights on disk")

    slot = ModelSlot(construct, seen.append)
    assert slot.get() is None
    assert slot.load_failed is True
    assert slot.get() is None  # the cached failure short-circuits
    assert attempts == [1]  # one attempt, not one per query
    assert [str(e) for e in seen] == ["no weights on disk"]


def test_model_slot_uses_a_lent_model_without_constructing() -> None:
    slot = ModelSlot(lambda: pytest.fail("must not construct"), model="LENT")
    assert slot.get() == "LENT"
    assert slot.load_failed is False


def test_model_slot_use_yields_the_loaded_model() -> None:
    with ModelSlot(lambda: "MODEL").use() as model:
        assert model == "MODEL"


def test_model_slot_use_yields_none_when_unavailable() -> None:
    # A failed construction: use() yields None, takes no use-lock, and a retry is
    # fine — a degraded model must not deadlock or strand the lock.
    slot = ModelSlot(_boom)
    with slot.use() as model:
        assert model is None
    with slot.use() as model:
        assert model is None


def test_model_slot_use_serializes_concurrent_callers() -> None:
    # Two threads must never hold the one shared model at once — the torch forward
    # race the use-lock exists to prevent. A enters and stays; B must block on the
    # use-lock until A leaves.
    slot = ModelSlot(lambda: "MODEL")
    a_inside = threading.Event()
    let_a_leave = threading.Event()
    b_inside = threading.Event()

    def hold_a() -> None:
        with slot.use() as model:
            assert model == "MODEL"
            a_inside.set()
            let_a_leave.wait(2.0)

    def enter_b() -> None:
        with slot.use():
            b_inside.set()

    ta = threading.Thread(target=hold_a)
    tb = threading.Thread(target=enter_b)
    ta.start()
    assert a_inside.wait(2.0)          # A holds the use-lock
    tb.start()
    assert not b_inside.wait(0.2)      # B blocked while A holds it (would enter at once if unguarded)
    let_a_leave.set()                  # release A
    assert b_inside.wait(2.0)          # now B gets in
    ta.join(2.0)
    tb.join(2.0)


def test_embedder_serializes_concurrent_encode(monkeypatch) -> None:
    # The real Embedder._encode path (via embed_query), driven from many threads
    # over one stand-in model, must never overlap two encodes — the concurrent
    # forward pass that corrupts a shared torch model's length-sized buffers (the
    # `size of tensor a must match tensor b` failure seen under `mine --jobs 5`).
    _models_on(monkeypatch)

    class _OverlapCheckModel:
        def __init__(self) -> None:
            self.inside = 0
            self.max_inside = 0
            self._lk = threading.Lock()

        def encode(self, prefixed, normalize_embeddings, convert_to_numpy,
                   show_progress_bar=False):
            with self._lk:
                self.inside += 1
                self.max_inside = max(self.max_inside, self.inside)
            time.sleep(0.002)  # widen the window a real forward pass would open
            with self._lk:
                self.inside -= 1
            return np.ones((len(prefixed), 3), dtype=np.float32)

    model = _OverlapCheckModel()
    embedder = embed.Embedder(load=lambda: model)
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(embedder.embed_query, [f"q{i}" for i in range(40)]))
    assert all(r is not None for r in results)
    assert model.max_inside == 1  # serialized — never two encodes at once


def test_build_model_pins_the_revision_and_trusts_remote_code(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cpu")
    made = []

    class RecordingST:
        def __init__(self, name, revision=None, trust_remote_code=False, device=None,
                     model_kwargs=None):
            made.append((name, revision, trust_remote_code, device, model_kwargs))

        def get_sentence_embedding_dimension(self):
            return 768

    model = embed.build_model(RecordingST, embed.DEFAULT_MODEL)
    assert isinstance(model, RecordingST)
    # trust_remote_code runs repo-hosted loader code, so the default model must load
    # at its pinned snapshot — a floating revision executes whatever upstream pushes.
    assert embed.PINNED_REVISIONS[embed.DEFAULT_MODEL]
    assert made == [(
        embed.DEFAULT_MODEL, embed.PINNED_REVISIONS[embed.DEFAULT_MODEL], True, "cpu",
        {},
    )]


def test_embed_build_model_applies_the_accelerator_dtype(monkeypatch) -> None:
    """The accelerator dtype policy is what keeps a cold corpus embedding in an
    hour rather than several — an fp32 load on an accelerator is the regression
    this pins against."""
    torch = pytest.importorskip("torch")
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "mps")
    made = []

    class RecordingST:
        def __init__(self, name, revision=None, trust_remote_code=False, device=None,
                     model_kwargs=None):
            made.append((device, model_kwargs))

    embed.build_model(RecordingST, embed.DEFAULT_MODEL)
    assert made == [("mps", {"torch_dtype": torch.float16})]


def test_build_model_reads_either_dimension_accessor(monkeypatch) -> None:
    """The dimension is a log detail, and sentence-transformers renamed its accessor:
    a model carrying either name — or neither — must still load."""
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED_DEVICE", "cpu")

    class NewerST:
        def __init__(self, name, **kw):
            pass

        def get_embedding_dimension(self):
            return 768

    class OlderST:
        def __init__(self, name, **kw):
            pass

        def get_sentence_embedding_dimension(self):
            return 768

    class DimlessST:
        def __init__(self, name, **kw):
            pass

    for cls in (NewerST, OlderST, DimlessST):
        assert isinstance(embed.build_model(cls, "acme/model"), cls)


# ── the embedder ──────────────────────────────────────────────────────────────
def test_embedder_availability_sequence(monkeypatch) -> None:
    _models_on(monkeypatch)
    # A lent model is available outright — it needs neither the extra nor a load.
    lent = embed.Embedder(model=_ScriptedModel())
    assert lent.is_available() is True
    # A load failure is remembered, so the arm degrades after exactly one attempt.
    broken = embed.Embedder(load=_boom)
    assert broken.is_available() is True  # nothing tried yet
    assert broken.embed_query("hi") is None  # the attempt fails…
    assert broken.is_available() is False  # …and is cached
    # The off switch stands every embedder down, loaded model or not.
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    assert lent.is_available() is False
    assert broken.is_available() is False


def test_off_switch_stops_the_embedder_before_any_load(monkeypatch) -> None:
    """The model-free pin / --lexical-only contract: no load may even be attempted."""
    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    e = embed.Embedder(load=lambda: pytest.fail("model load attempted"))
    assert e.is_available() is False
    assert e.warm() is False
    assert e.embed_query("anything at all") is None
    assert e.embed_documents(["doc"]) is None


def test_embedder_degrades_after_one_failed_load(monkeypatch) -> None:
    _models_on(monkeypatch)
    attempts: list[int] = []

    def construct() -> object:
        attempts.append(1)
        raise RuntimeError("no weights on disk")

    e = embed.Embedder(load=construct)
    assert e.embed_query("hi") is None
    assert e.embed_documents(["doc"]) is None
    assert attempts == [1]  # one load attempt, then the cached failure


def test_a_document_batch_encodes_in_lock_sized_chunks(monkeypatch) -> None:
    """The model's use-lock serializes forward passes, so an indexing batch and a
    search query contend for it and the *query* waits out whatever pass is in
    flight. One pass over a whole 256-doc batch holds it for seconds — which
    reaches a search as an embed that took seconds on a long-warm process — so the
    batch is encoded a chunk at a time and the lock is released between chunks.

    Pinned through the model's own view: how many passes it was asked for and how
    wide each was. The vectors are unchanged by the split (encoding is per
    document), so order and count are pinned here too — a chunked path that
    silently reordered or dropped a document would corrupt the index."""
    _models_on(monkeypatch)

    class _IndexedModel(_ScriptedModel):
        """Returns a vector naming its input's position, so a reorder is visible."""

        def encode(self, prefixed, **kw):
            super().encode(prefixed, **kw)
            return np.asarray([[float(p.rsplit(" ", 1)[-1])] * self.width
                               for p in prefixed], dtype=self.dtype)

    model = _IndexedModel(width=2)
    e = embed.Embedder(model=model)
    n = embed.EMBED_BATCH_CHUNK * 2 + 3
    out = e.embed_documents([f"doc {i}" for i in range(n)])

    assert [v[0] for v in out] == [float(i) for i in range(n)], "the batch came back reordered"
    assert len(model.seen) == 3, "the batch was not split across passes"
    assert [len(call) for call in model.seen] == [
        embed.EMBED_BATCH_CHUNK, embed.EMBED_BATCH_CHUNK, 3]
    # A query is one string, so it stays one pass and pays nothing for the loop.
    model.seen.clear()
    e.embed_query("a question")
    assert len(model.seen) == 1


def test_embed_query_prefixes_caps_and_short_circuits(monkeypatch) -> None:
    _models_on(monkeypatch)
    model = _ScriptedModel(width=2)
    e = embed.Embedder(model=model)
    assert e.embed_query("  hello  ") == [1.0, 1.0]
    assert model.seen[0][0].startswith("search_query: ")
    long = "z" * (embed.EMBEDDING_CHAR_CAP + 50)
    e.embed_query(long)
    assert len(model.seen[1][0]) == len("search_query: ") + embed.EMBEDDING_CHAR_CAP
    assert e.embed_query("   ") is None
    assert e.embed_query("") is None
    assert len(model.seen) == 2  # a blank query never reaches the model


def test_a_repeated_query_reuses_its_vector(monkeypatch) -> None:
    """The same query text embeds to the same vector for as long as one model is
    loaded, so a repeat must not pay a second forward pass. Paging is what makes
    this the common case: every page of a walk re-embeds one identical string."""
    _models_on(monkeypatch)
    model = _ScriptedModel(width=2)
    e = embed.Embedder(model=model)

    first = e.embed_query("a repeated ask")
    for _ in range(5):
        assert e.embed_query("a repeated ask") == first
    assert len(model.seen) == 1, "a cached query went back to the model"
    assert e.query_cache_stats() == {"entries": 1, "hits": 5, "misses": 1}

    e.embed_query("a different ask")
    assert len(model.seen) == 2, "a distinct query must still reach the model"


def test_a_cache_hit_tells_the_probe_it_happened(monkeypatch) -> None:
    """The cache makes ``embed_ms`` ~0 on an arm that ran, which is the same number
    an arm that never embedded reports. The embedder states the difference at the
    only place that knows it."""
    from thread_archive._retrieval import _probe

    _models_on(monkeypatch)
    e = embed.Embedder(model=_ScriptedModel(width=2))

    with _probe.install() as miss:
        e.embed_query("first time")
    assert miss.embed_cached is False, "a miss claimed a cache hit"

    with _probe.install() as hit:
        e.embed_query("first time")
    assert hit.embed_cached is True


def test_a_deferred_model_says_so_rather_than_leaving_it_inferred(monkeypatch) -> None:
    """The model half of the deferral fork. A warming server sits the vector arm out
    until its model is resident, and that ``None`` reaches the caller looking exactly
    like models-off or a cached load failure. The flag is set where the reason is
    known — and the search is *not* cold, because no load was paid."""
    from thread_archive._retrieval import _probe
    from thread_archive._retrieval.model_slot import set_defer_construction

    _models_on(monkeypatch)
    loads: list[int] = []

    def _load():
        loads.append(1)
        return _ScriptedModel(width=2)

    e = embed.Embedder(load=_load)
    set_defer_construction(True)
    try:
        with _probe.install() as deferred:
            assert e.embed_query("racing the warm") is None
        assert deferred.embed_deferred is True
        assert deferred.as_record()["embed_deferred"] is True
        assert not loads, "a deferring request path constructed the model"
        # Not the cold-model tail: nothing was loaded, so nothing was paid for.
        assert deferred.cold is False
        assert "cold" not in deferred.as_record()

        # Warming is what the policy defers *to* — once it lands the arm rejoins and
        # the flag stops being set, since it names a window rather than a config.
        assert e.warm() is True
        with _probe.install() as rejoined:
            assert e.embed_query("racing the warm") is not None
        assert rejoined.embed_deferred is False
        assert "embed_deferred" not in rejoined.as_record()
    finally:
        set_defer_construction(False)


def test_a_lexical_only_install_is_not_a_deferral(monkeypatch) -> None:
    """The flag has to separate "wait a few seconds" from "this install has no vector
    arm at all" — the two degrade identically at the call site and want opposite
    responses, which is the whole reason for stating the reason."""
    from thread_archive._retrieval import _probe
    from thread_archive._retrieval.model_slot import set_defer_construction

    monkeypatch.setenv("THREAD_ARCHIVE_EMBED", "off")
    e = embed.Embedder(load=lambda: pytest.fail("model load attempted"))
    set_defer_construction(True)
    try:
        with _probe.install() as probe:
            assert e.embed_query("anything") is None
        assert probe.embed_deferred is False, "an off switch read as a warm window"
    finally:
        set_defer_construction(False)


def test_the_query_cache_hands_out_copies(monkeypatch) -> None:
    """The vector travels into arithmetic the caller owns. A shared list would let
    one caller's in-place normalize poison the entry for every later query."""
    _models_on(monkeypatch)
    e = embed.Embedder(model=_ScriptedModel(width=2))

    got = e.embed_query("mutate me")
    got[0] = 999.0
    assert e.embed_query("mutate me") == [1.0, 1.0], "a caller edited the cached vector"


def test_the_query_cache_evicts_and_never_caches_a_failure(monkeypatch) -> None:
    """Bounded, least-recently-used first — and a ``None`` is never stored. Every
    way an embed returns None (models off, load failed, deferred construction) is a
    condition that resolves, so caching one would pin a transient state for the
    life of the process."""
    _models_on(monkeypatch)
    model = _ScriptedModel(width=2)
    e = embed.Embedder(model=model, query_cache_max=2)

    e.embed_query("one")
    e.embed_query("two")
    e.embed_query("one")            # refreshes 'one', so 'two' is now the oldest
    e.embed_query("three")          # evicts 'two'
    assert e.query_cache_stats()["entries"] == 2
    seen = len(model.seen)
    e.embed_query("one")
    assert len(model.seen) == seen, "the recently-used entry was evicted"
    e.embed_query("two")
    assert len(model.seen) == seen + 1, "the least-recently-used entry survived"

    broken = embed.Embedder(load=_boom)
    assert broken.embed_query("never cached") is None
    assert broken.query_cache_stats()["entries"] == 0


def test_embed_documents_prefixes_caps_and_empties(monkeypatch) -> None:
    _models_on(monkeypatch)
    model = _ScriptedModel(width=1)
    e = embed.Embedder(model=model)
    long = "z" * (embed.EMBEDDING_CHAR_CAP + 50)
    assert e.embed_documents([long, "b"]) == [[1.0], [1.0]]
    assert model.seen[0][0].startswith("search_document: ")
    assert len(model.seen[0][0]) == len("search_document: ") + embed.EMBEDDING_CHAR_CAP
    assert model.seen[0][1] == "search_document: b"
    assert e.embed_documents([]) is None
    assert len(model.seen) == 1  # an empty batch never reaches the model


def test_encode_casts_to_float32_lists(monkeypatch) -> None:
    _models_on(monkeypatch)
    e = embed.Embedder(model=_ScriptedModel(width=4, dtype=np.float64))
    out = e.embed_documents(["a", "b"])
    assert out == [[1.0] * 4, [1.0] * 4]
    assert all(isinstance(x, float) for row in out for x in row)


def test_encode_swallows_a_model_error(monkeypatch) -> None:
    _models_on(monkeypatch)

    class Boom:
        def encode(self, *a, **k):
            raise RuntimeError("cuda oom")

    e = embed.Embedder(model=Boom())
    assert e.embed_query("x") is None
    assert e.embed_documents(["x"]) is None


def test_warm_reports_the_load_outcome(monkeypatch) -> None:
    _models_on(monkeypatch)
    assert embed.Embedder(model=_ScriptedModel()).warm() is True
    assert embed.Embedder(load=_boom).warm() is False


def test_default_embedder_is_process_wide(monkeypatch) -> None:
    assert embed.default() is embed.default()
    _models_on(monkeypatch)
    # The module-level functions are the default instance's, not a second one.
    assert embed.is_available() is embed.default().is_available()


# ══════════════════════════════════════════════════════════════════════════════
class _OtherDialectEngine:
    """A store on a dialect the vector arm doesn't serve. Stated rather than dialled:
    no non-SQLite driver is installed here, and the guards read only the dialect."""

    dialect = SimpleNamespace(name="postgresql")


class _BrokenEngine:
    """A store whose engine is unusable — the guards must degrade, not propagate."""

    @property
    def dialect(self):
        raise RuntimeError("engine down")


def test_is_available_false_on_engine_error() -> None:
    with use_engine(_BrokenEngine()):
        assert vectors.is_available() is False


def test_unavailable_guards_short_circuit(archive_home, tmp_path) -> None:
    """On a non-SQLite store every entry point degrades rather than emitting
    SQLite-only SQL at it."""
    truth = tmp_path / "truth"
    truth.mkdir()
    with use_engine(_OtherDialectEngine()):
        assert vectors.is_available() is False
        assert vectors.ensure_index() is False
        assert vectors.index_vectors([(1, "user", _unit((0, 1.0)))]) == 0
        assert vectors.index_events_local() == 0
        assert vectors.save_vectors_sidecar(truth) == 0
        assert vectors.load_vectors_sidecar(truth) == 0
        assert vectors.search("hi") is None
        assert vectors.get_status() == {"available": False}


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
    # conftest's model-free pin leaves the process embedder unavailable → the vector
    # store is SQLite but the embed backend sits out → 0 docs embedded.
    assert vectors.index_events_local() == 0


def test_index_events_local_stops_when_embed_returns_none(archive_home) -> None:
    import_cc_session(archive_home)

    class _FailingEmbedder(_FixedEmbedder):
        def embed_documents(self, texts):
            return None

    # batch_size=1 forces a flush on the first pending doc; the None embed return
    # stops the pass mid-loop with nothing written.
    assert vectors.index_events_local(
        batch_size=1, embedder=_FailingEmbedder(_unit((0, 1.0)))) == 0
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


def test_matrix_serves_stale_within_cooldown(archive_home) -> None:
    """A cached matrix is served as-is inside the cooldown: an insert bumps the
    validity token, but the request thread never pays the rebuild — the lexical arm
    covers the freshest, not-yet-repacked vectors."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    ids1, *_ = vectors._load_matrix(("user",))  # cold: builds inline
    assert len(ids1) == 1
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])
    ids2, *_ = vectors._load_matrix(("user",))  # inside cooldown → stale served
    assert len(ids2) == 1


def test_matrix_refresh_picks_up_writes(archive_home) -> None:
    """The single-flight refresh rebuilds from the live store; the rebuilt entry then
    serves the new vectors. Driven synchronously so the assertion can't race the
    background thread that runs this same body."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    key = vectors._matrix_key(("user",))
    vectors._load_matrix(("user",))  # cold build → 1-row entry cached
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])
    vectors._refresh_matrix(key, ("user",))
    ids, *_ = vectors._load_matrix(("user",))
    assert len(ids) == 2


def _wait_for_refresh(key, timeout: float = 10.0) -> None:
    """Block until the single-flight background matrix refresh for ``key`` clears."""
    marker = vectors._inflight_marker(current_archive(), key)
    deadline = time.monotonic() + timeout
    while marker in vectors._MATRIX_REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)


def test_load_matrix_background_refresh_past_cooldown(archive_home) -> None:
    """Past the cooldown, a search kicks the real single-flight background refresh,
    which rebuilds from the live store; the next search then serves the new
    vectors (the request thread never blocks on the rebuild)."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    key = vectors._matrix_key(("user",))
    ids1, *_ = vectors._load_matrix(("user",))  # cold build → 1 row
    assert len(ids1) == 1
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])
    # Age the last-checked stamp (plain write to test-owned state) so the next load
    # treats the cooldown as elapsed and schedules the real refresh thread.
    current_archive().cache(vectors._CHECKED_SLOT)[key] = 0.0
    vectors._load_matrix(("user",))  # serves stale, kicks the background refresh
    _wait_for_refresh(key)
    ids2, *_ = vectors._load_matrix(("user",))
    assert len(ids2) == 2  # the landed refresh now serves both


def test_refresh_matrix_async_single_flight_suppresses_duplicate(archive_home) -> None:
    """A refresh already in flight for a key suppresses another — one rebuild, not N,
    when a burst of queries races the same moved token. With a rebuild marked in
    flight, a second async request is a no-op and the stale entry stands; once the
    marker clears, the async refresh runs and picks up the write."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    key = vectors._matrix_key(("user",))
    vectors._load_matrix(("user",))  # cache holds the 1-row matrix
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])

    marker = vectors._inflight_marker(current_archive(), key)
    vectors._MATRIX_REFRESHING.add(marker)  # a rebuild is (nominally) already in flight
    try:
        vectors._refresh_matrix_async(key, ("user",))  # suppressed — no rebuild
        ids, *_ = vectors._load_matrix(("user",))
        assert len(ids) == 1  # still stale: the guard blocked the rebuild
    finally:
        vectors._MATRIX_REFRESHING.discard(marker)

    vectors._refresh_matrix_async(key, ("user",))  # guard clear → runs for real
    _wait_for_refresh(key)
    ids, *_ = vectors._load_matrix(("user",))
    assert len(ids) == 2


def test_reset_matrix_cache_forces_live_rebuild(archive_home) -> None:
    """``reset_matrix_cache`` drops the cache so the next search rebuilds from the
    live store — the promptness reindex relies on (never serving dead rows)."""
    init_db()
    vectors.ensure_index()
    vectors.reset_matrix_cache()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    vectors._load_matrix(("user",))  # cache holds the 1-row matrix
    vectors.index_vectors([(2, "user", _unit((1, 1.0)))])
    vectors.reset_matrix_cache()
    ids, *_ = vectors._load_matrix(("user",))  # cold rebuild reflects the live store
    assert len(ids) == 2


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


def test_save_sidecar_defaults_to_the_process_embedding_space(archive_home, tmp_path) -> None:
    """Without an explicit key the sidecar is tagged with the process embedder's
    space, so a model change invalidates it on the next load."""
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    truth = tmp_path / "truth"
    truth.mkdir()
    assert vectors.save_vectors_sidecar(truth) == 1
    assert vectors.load_vectors_sidecar(truth) == 1  # same space → restored
    assert vectors.load_vectors_sidecar(truth, space_key="local:other") == 0


def test_save_sidecar_ignores_and_sweeps_stray_builds(archive_home, tmp_path) -> None:
    """Concurrent/dead savers' build files must not break a save: each saver
    builds under its own pid-unique name, a stale stray (a crashed build —
    the shape that would collide on CREATE TABLE) is swept by age, and a
    fresh stray (a live concurrent build) is left alone."""
    import sqlite3
    import time

    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    truth = tmp_path / "truth"
    truth.mkdir()
    stale = truth / "vectors.sqlite.tmp"  # old fixed-name build, saver long dead
    con = sqlite3.connect(stale)
    con.execute("CREATE TABLE event_vectors (x)")
    con.commit()
    con.close()
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = truth / "vectors.sqlite.tmp.99999999"  # a concurrent saver, mid-build
    fresh.write_bytes(b"")

    assert vectors.save_vectors_sidecar(truth, space_key="local:test") == 1
    assert not stale.exists()
    assert fresh.exists()
    assert vectors.load_vectors_sidecar(truth, space_key="local:test") == 1


def test_failed_save_cleans_its_build_file(archive_home, tmp_path) -> None:
    """A publish that can't complete must not leave its build file behind for the
    stale sweep to age out. Driven by a real failing rename: the destination name
    is occupied by a directory, so os.replace raises for real."""
    init_db()
    vectors.ensure_index()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    truth = tmp_path / "truth"
    truth.mkdir()
    (truth / "vectors.sqlite").mkdir()  # publishing over a directory cannot work

    with pytest.raises(OSError):
        vectors.save_vectors_sidecar(truth, space_key="local:test")
    assert list(truth.glob("vectors.sqlite.tmp*")) == []


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


# ── search: the full embed→KNN→hydrate path (query embedder injected) ─────────
def _index_user_and_text(qvec) -> None:
    """Index one vector per embeddable events_fts row using ``qvec``."""
    with get_session() as s:
        rows = s.execute(sa_text(
            "SELECT event_id, content_type FROM events_fts "
            "WHERE content_type IN ('user', 'text') AND content != ''"
        )).all()
    vectors.index_vectors([(int(eid), ct, qvec) for eid, ct in rows])


def _seeded(archive_home):
    """A store whose embeddable rows all carry one vector, plus the embedder that
    answers queries with the same vector — so KNN scores an exact match."""
    import_cc_session(archive_home)
    q = _unit((0, 1.0))
    _index_user_and_text(q)
    return _FixedEmbedder(q.tolist())


def test_search_unscoped_hydrates_hits(archive_home) -> None:
    emb = _seeded(archive_home)
    hits = vectors.search("hello", embedder=emb)
    assert hits is not None and len(hits) >= 1
    assert all("_semantic" in h for h in hits)
    assert hits[0]["_semantic"] == 1.0  # identical vector → cosine 1
    assert emb.queries == ["hello"]  # the caller's embedder did the embedding


def test_search_scoped_by_thread(archive_home) -> None:
    emb = _seeded(archive_home)
    with get_session() as s:
        tid = s.execute(sa_text("SELECT id FROM threads LIMIT 1")).scalar()
    hits = vectors.search("hello", thread_id=tid, embedder=emb)
    assert hits and all(h["thread_id"] == tid for h in hits)


def test_search_scoped_thread_without_events_is_empty(archive_home) -> None:
    emb = _seeded(archive_home)
    assert vectors.search("hello", thread_id=999_999, embedder=emb) == []


def test_search_source_and_time_window(archive_home) -> None:
    emb = _seeded(archive_home)
    with get_session() as s:
        src = s.execute(sa_text("SELECT source FROM threads LIMIT 1")).scalar()
    hits = vectors.search(
        "hello", source=[src], embedder=emb,
        since="2020-01-01T00:00:00Z", until="2030-01-01T00:00:00Z",
    )
    assert hits and len(hits) >= 1
    # An unknown source scopes to no events → empty (not None).
    assert vectors.search("hello", source=["no-such-source"], embedder=emb) == []


def test_search_partial_exclude_drops_type_in_hydration(archive_home) -> None:
    emb = _seeded(archive_home)
    hits = vectors.search("hello", exclude_content_types=["text"], embedder=emb)
    assert hits is not None
    assert all(h["content_type"] != "text" for h in hits)


def test_search_exclude_all_embedded_returns_none(archive_home) -> None:
    emb = _seeded(archive_home)
    assert vectors.search(
        "hi", exclude_content_types=["user", "text", "title"], embedder=emb
    ) is None
    assert emb.queries == []  # sat out before embedding


def test_search_empty_scope_when_no_candidates(archive_home) -> None:
    emb = _seeded(archive_home)  # only user/text vectors exist
    # Scope to 'title' (no vectors) → KNN finds nothing → [].
    assert vectors.search("hello", content_types=["title"], embedder=emb) == []


def test_search_none_on_empty_query(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    assert vectors.search("") is None
    assert vectors.search("   ") is None


def test_search_falls_back_to_the_process_embedder(archive_home) -> None:
    """Without an explicit embedder the arm uses the process one — which the
    model-free pin has stood down, so the search degrades instead of loading."""
    _seeded(archive_home)  # a pool exists, so the arm gets as far as embedding
    assert embed.is_available() is False
    assert vectors.search("hello") is None


def test_search_none_when_nothing_indexed(archive_home) -> None:
    init_db()
    vectors.ensure_index()
    emb = _FixedEmbedder(_unit((0, 1.0)).tolist())
    assert vectors.search("anything", embedder=emb) is None
    assert emb.queries == []  # nothing indexed → never embeds the query


def test_search_none_when_query_embed_returns_none(archive_home) -> None:
    emb = _seeded(archive_home)

    class _NoQueryVector(_FixedEmbedder):
        def embed_query(self, text):
            return None

    assert vectors.search("hello", embedder=_NoQueryVector(emb.vec)) is None


def test_search_none_when_query_embed_raises(archive_home) -> None:
    emb = _seeded(archive_home)

    class _ExplodingEmbedder(_FixedEmbedder):
        def embed_query(self, text):
            raise RuntimeError("embed exploded")

    assert vectors.search("hello", embedder=_ExplodingEmbedder(emb.vec)) is None


def test_search_hydration_skips_row_without_candidate_sim(archive_home) -> None:
    emb = _seeded(archive_home)
    # Inject an extra events_fts row for the user event under a content_type this
    # fixture never embedded (no vector → never a KNN candidate). Hydration selects
    # by event_id, not content_type, so the row surfaces but has no candidate
    # similarity and is skipped.
    with get_session() as s:
        uid = int(s.execute(sa_text(
            "SELECT event_id FROM events_fts WHERE content_type='user' LIMIT 1"
        )).scalar())
        tid = s.execute(sa_text(
            "SELECT thread_id FROM events_fts WHERE event_id=:e LIMIT 1"), {"e": uid}
        ).scalar()
        s.execute(sa_text(
            "INSERT INTO events_fts "
            "(event_id, thread_id, event_type, content, content_type, tool_name) "
            "VALUES (:event_id, :thread_id, :event_type, :content, :content_type, :tool_name)"
        ), {"event_id": uid, "thread_id": tid, "event_type": "thread_meta",
            "content": "injected meta row", "content_type": "title", "tool_name": None})
        s.commit()
    hits = vectors.search("hello", embedder=emb)
    assert hits is not None
    assert all(h["content_type"] != "title" for h in hits)  # injected row skipped


# ── the in-process embed drain (embedder injected) ────────────────────────────
def test_index_events_local_full_drain(archive_home) -> None:
    import_cc_session(archive_home)  # 1 user + 1 assistant-text = 2 embeddable docs
    emb = _FixedEmbedder(_unit((0, 1.0)).tolist())
    assert vectors.index_events_local(embedder=emb) == 2  # one end-of-loop flush drains both
    assert vectors.get_status()["indexed"] == 2
    assert vectors.index_events_local(embedder=emb) == 0  # caught up → final flush no-op


def test_index_events_local_capped_newest_first(archive_home) -> None:
    import_cc_session(archive_home)
    emb = _FixedEmbedder(_unit((0, 1.0)).tolist())
    assert vectors.index_events_local(max_events=1, newest_first=True, embedder=emb) == 1
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
    current_archive().cache(vectors._MATRIX_SLOT).clear()
    vectors.index_vectors([(1, "user", _unit((0, 1.0)))])
    first = vectors._load_matrix(("user",))
    second = vectors._load_matrix(("user",))  # unchanged token → cache hit
    assert second[0] is first[0]  # same cached ndarray object returned
