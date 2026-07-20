"""In-process nomic embeddings (sentence-transformers) — the `[embeddings]` extra.

The in-process embedding server: nomic model, ``search_query:`` /
``search_document:`` prefixes, 768-dim ``list[float]`` out, ``None`` on failure.
Heavy (torch); gated to the extra. The base install is lexical-only:
``is_available()`` is False and the vector arm degrades out cleanly.

An :class:`Embedder` owns one model plus the policy for querying it. The
module-level functions delegate to the process default (:func:`default`);
anything that consumes embeddings takes an ``embedder`` argument, so a caller
with its own — a host process lending an already-loaded model, a second space
during a re-embed, a scripted stand-in — passes it in rather than replacing the
default.

Self-consistency is the contract that matters — vectors indexed with one embedder
must be *queried* with it (llama.cpp-nomic ≠ torch-nomic are different spaces). The
``space_key`` tags a vector set by model so a cached set is only reused in-space.

``$THREAD_ARCHIVE_EMBED=off`` pins a process lexical-only: every embedder reports
unavailable, so no model loads and search runs on the lexical arm alone. It is the
switch for a box that has the extra installed but wants search cheap and cold-start
free (``retrieval_eval.py --lexical-only`` sets it for the run it measures).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from collections.abc import Callable
from typing import Any, Optional, Protocol

from .model_slot import ModelSlot

logger = logging.getLogger(__name__)


class SentenceEncoder(Protocol):
    """All an embedder needs of a loaded model: batch-encode text to vectors. A
    sentence-transformers ``SentenceTransformer`` satisfies it, and so does anything
    else that encodes the same way."""

    def encode(self, sentences: list[str], normalize_embeddings: bool,
               convert_to_numpy: bool) -> Any: ...


# Cap input length before embedding. On the in-process torch path a batch of many
# 6000-char docs (≈1500 tokens each) spikes MPS memory — it hangs and crawls
# (~9 docs/s). 2048 chars keeps the gist of all but the longest code/tool dumps
# (mean doc is ~300 chars) and runs ~7× faster with no hang.
EMBEDDING_CHAR_CAP = 2048

DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"

# Pinned upstream snapshots, by model. The nomic loader executes repo-hosted code
# (``trust_remote_code``), so a floating revision would let an upstream push run
# new code over a store of private conversations. Bump deliberately when
# upgrading a model. A model that isn't listed floats unless
# ``THREAD_ARCHIVE_EMBED_REVISION`` pins it — a pin belongs to the model it names.
PINNED_REVISIONS = {DEFAULT_MODEL: "e9b6763023c676ca8431644204f50c2b100d9aab"}

# Spellings of "off" accepted by the model switches, so an operator's habitual
# form works rather than silently leaving the models on.
_OFF = frozenset({"0", "off", "false", "no"})


def models_enabled(var: str = "THREAD_ARCHIVE_EMBED") -> bool:
    """False when ``$THREAD_ARCHIVE_EMBED`` is set to an off value (``off``, ``0``,
    ``false``, ``no``) — the switch that pins a process to lexical search without
    uninstalling the ``[embeddings]`` extra. Read per call, so it can be set for a
    single command or subprocess. ``var`` names the switch, so the re-rank stage
    reads its own with the same spellings."""
    return os.environ.get(var, "").strip().lower() not in _OFF


def importable(name: str) -> bool:
    """True when ``name`` has an import spec, without importing it. False when the
    lookup itself fails — a missing parent package raises rather than answering."""
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:
        return False


def model_name() -> str:
    """The configured embedding model: ``$THREAD_ARCHIVE_EMBED_MODEL`` or the default."""
    return os.environ.get("THREAD_ARCHIVE_EMBED_MODEL") or DEFAULT_MODEL


def revision_for(name: str) -> Optional[str]:
    """The revision to load ``name`` at: ``$THREAD_ARCHIVE_EMBED_REVISION`` if set,
    else the model's pinned snapshot, else None (floating)."""
    return os.environ.get("THREAD_ARCHIVE_EMBED_REVISION") or PINNED_REVISIONS.get(name)


def space_key(name: Optional[str] = None) -> str:
    """Identity of an embedding space — vectors are only comparable within one."""
    return "local:" + (name or model_name())


# ── device selection ──────────────────────────────────────────────────────────
def select_device(override: Optional[str], has_mps: bool, has_cuda: bool) -> str:
    """The device to run on: an explicit override wins, else the best accelerator,
    else CPU. On Apple silicon the GPU is ~20× the CPU — the difference between an
    hour and a day for the corpus embed."""
    if override:
        return override
    if has_mps:
        return "mps"
    if has_cuda:
        return "cuda"
    return "cpu"


def torch_accelerators() -> tuple[bool, bool]:
    """``(mps, cuda)`` availability. Both False when torch isn't importable or its
    probes raise — a box without the extra runs on CPU."""
    try:
        import torch

        return bool(torch.backends.mps.is_available()), bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False, False


def _device() -> str:
    """``$THREAD_ARCHIVE_EMBED_DEVICE`` if set, else the best accelerator."""
    return select_device(os.environ.get("THREAD_ARCHIVE_EMBED_DEVICE"), *torch_accelerators())


# ── hub cache / offline pinning ───────────────────────────────────────────────
def _hub_cache_dir() -> str:
    """The HF hub cache directory, resolved the way huggingface_hub does — but as a pure
    filesystem path, with no `huggingface_hub` import. Called *before* we set the offline
    env, so it must not pull in the hub (which freezes ``HF_HUB_OFFLINE`` at import)."""
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return os.environ["HUGGINGFACE_HUB_CACHE"]
    if os.environ.get("HF_HOME"):
        return os.path.join(os.environ["HF_HOME"], "hub")
    return os.path.expanduser("~/.cache/huggingface/hub")


def _model_cached(name: str) -> bool:
    """True when ``name`` is already in the HF hub cache (a non-empty ``snapshots/``).
    Pure filesystem probe — no hub import — so it's safe to consult before going offline."""
    folder = "models--" + name.replace("/", "--")
    snaps = os.path.join(_hub_cache_dir(), folder, "snapshots")
    try:
        return os.path.isdir(snaps) and any(os.scandir(snaps))
    except OSError:
        return False


def _pin_offline_if_cached(name: str) -> None:
    """Pin the model load to the local cache once it's downloaded, so the hot path makes
    NO Hub request: no revision re-resolve, no ``trust_remote_code`` re-fetch, no
    unauthenticated-HF-Hub warning, and it still works with the network down. The model is
    pinned (one fixed name) — a Hub round-trip on every search buys nothing.

    Skipped when the model isn't cached yet (so a first install still downloads it) or when
    ``THREAD_ARCHIVE_EMBED_ONLINE=1`` forces an online load (first download / deliberate
    refresh). ``huggingface_hub`` freezes ``HF_HUB_OFFLINE`` into a module constant at
    import, so set the env *before* it's imported and, if it already is, flip the live
    constant too."""
    if os.environ.get("THREAD_ARCHIVE_EMBED_ONLINE") == "1" or not _model_cached(name):
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    const = sys.modules.get("huggingface_hub.constants")
    if const is not None:
        setattr(const, "HF_HUB_OFFLINE", True)


def build_model(sentence_transformer: Callable[..., SentenceEncoder], name: str) -> SentenceEncoder:
    """Instantiate ``sentence_transformer`` for ``name`` on the chosen device, pinned to
    its revision (nomic needs ``trust_remote_code``, which is exactly why the revision is
    pinned). Split from the import so the construction policy is one readable call."""
    device = _device()
    model = sentence_transformer(
        name, revision=revision_for(name),
        trust_remote_code=True, device=device,
    )
    # sentence-transformers renamed the accessor; take whichever this model has.
    # Only a log detail, so a model carrying neither still loads.
    dim = next(
        (getattr(model, a)() for a in ("get_embedding_dimension",
                                       "get_sentence_embedding_dimension")
         if callable(getattr(model, a, None))),
        None,
    )
    logger.info("embed: loaded %s (dim=%s, device=%s)", name, dim, device)
    return model


class Embedder:
    """One embedding model plus the policy the archive queries it with: the nomic
    ``search_query:`` / ``search_document:`` prefixes, the char cap, and the
    ``space_key`` that tags its vectors.

    The model loads lazily on first use and a failed load is cached, so an unloadable
    model costs one attempt and then degrades to ``None`` on every call. Pass ``model=``
    to use one that is already loaded — a host holding a SentenceTransformer lends it
    here instead of paying for a second half-gigabyte copy — or ``load=`` to supply a
    different loader, which then answers for its own dependencies rather than the
    ``[embeddings]`` extra.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        *,
        model: Optional[SentenceEncoder] = None,
        load: Optional[Callable[[], SentenceEncoder]] = None,
    ) -> None:
        self._name = name
        # The ``[embeddings]`` extra is a precondition for the loader this class
        # supplies itself, not for one it was handed.
        self._needs_extra = load is None
        self._slot: ModelSlot[SentenceEncoder] = ModelSlot(
            load or self._construct, self._degrade, model=model)

    @property
    def name(self) -> str:
        """The model this embedder loads: the one it was constructed with, else the
        configured one — so the process embedder follows ``$THREAD_ARCHIVE_EMBED_MODEL``
        rather than whichever value happened to be set when the module was imported."""
        return self._name or model_name()

    def _degrade(self, e: Exception) -> None:
        logger.warning("embed: model load failed (%s) — vector arm degrades", e)

    def _construct(self) -> SentenceEncoder:
        """Construct the SentenceTransformer.

        Raises on any failure — including the ``[embeddings]`` extra being absent —
        and the slot catches it, caching the failure so the vector arm degrades."""
        _pin_offline_if_cached(self.name)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # extra absent — degrade via the slot's failure cache
            raise RuntimeError(f"[embeddings] extra not installed: {e}") from e
        return build_model(SentenceTransformer, self.name)

    def space_key(self) -> str:
        """Identity of this embedder's space — vectors are only comparable within one."""
        return space_key(self.name)

    def is_available(self) -> bool:
        """True when this embedder can produce vectors: models aren't switched off, the
        model is loaded or loadable, and no load has already failed. Cheap — never loads
        the model; a load failure degrades at call time."""
        if not models_enabled():
            return False
        if self._slot.model is not None:
            return True
        if self._needs_extra and not importable("sentence_transformers"):
            return False
        return not self._slot.load_failed

    def warm(self) -> bool:
        """Eagerly load the model so it isn't cold-loaded inside the first query.
        Fail-soft and idempotent: returns False when the model is unavailable or the
        load fails (search then stays lexical, exactly as it does without warming)."""
        if not self.is_available():
            return False
        return self._slot.get() is not None

    def _encode(self, prefixed: list[str]) -> Optional[list[list[float]]]:
        # The documented degrade contract: is_available() False means the vector
        # arm sits out entirely. Checked here — not just in warm()/indexing — so a
        # caller that reaches encoding directly (query embedding via
        # vectors.search) honors it too, and a lexical-only run can never
        # cold-load a model.
        if not self.is_available():
            return None
        model = self._slot.get()
        if model is None:
            return None
        try:
            # Un-normalized to match the contract — the vector store normalizes on write.
            vecs = model.encode(prefixed, normalize_embeddings=False, convert_to_numpy=True)
            return [v.astype("float32").tolist() for v in vecs]
        except Exception as e:  # noqa: BLE001
            logger.warning("embed: encode failed (%s)", e)
            return None

    def embed_query(self, text: str) -> Optional[list[float]]:
        """Embed a search query (nomic ``search_query:`` prefix). None on any failure."""
        if not text or not text.strip():
            return None
        vecs = self._encode([f"search_query: {text[:EMBEDDING_CHAR_CAP]}"])
        return vecs[0] if vecs else None

    def embed_documents(self, texts: list[str]) -> Optional[list[list[float]]]:
        """Embed indexed content (nomic ``search_document:`` prefix), in input order. None
        on any failure (the caller writes nothing — never a partial/wrong-space batch)."""
        if not texts:
            return None
        return self._encode([f"search_document: {t[:EMBEDDING_CHAR_CAP]}" for t in texts])


# The process embedder: one model shared by every caller that doesn't bring its own.
_DEFAULT = Embedder()


def default() -> Embedder:
    """The process embedder — the one every ``embedder``-taking call falls back to."""
    return _DEFAULT


def is_available() -> bool:
    """True when the process embedder can produce vectors. Cheap — does not load."""
    return _DEFAULT.is_available()


def warm() -> bool:
    """Preload the process embedder. False when it's unavailable or the load fails."""
    return _DEFAULT.warm()


def embed_query(text: str) -> Optional[list[float]]:
    """Embed a search query with the process embedder. None on any failure."""
    return _DEFAULT.embed_query(text)


def embed_documents(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed indexed content with the process embedder. None on any failure."""
    return _DEFAULT.embed_documents(texts)
