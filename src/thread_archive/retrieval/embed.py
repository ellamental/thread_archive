"""In-process nomic embeddings (sentence-transformers) — the `[embeddings]` extra.

The in-process embedding server: nomic model, ``search_query:`` /
``search_document:`` prefixes, 768-dim ``list[float]`` out, ``None`` on failure.
Heavy (torch); gated to the extra. The base install is lexical-only:
``is_available()`` is False and the vector arm degrades out cleanly.

Self-consistency is the contract that matters — vectors indexed with this provider
must be *queried* with it (llama.cpp-nomic ≠ torch-nomic are different spaces). The
``space_key`` tags a vector set by model so a cached set is only reused in-space.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Cap input length before embedding. On the in-process torch path a batch of many
# 6000-char docs (≈1500 tokens each) spikes MPS memory — it hangs and crawls
# (~9 docs/s). 2048 chars keeps the gist of all but the longest code/tool dumps
# (mean doc is ~300 chars) and runs ~7× faster with no hang.
EMBEDDING_CHAR_CAP = 2048
_MODEL_NAME = os.environ.get("THREAD_ARCHIVE_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")

_model = None
_load_failed = False


def model_name() -> str:
    return _MODEL_NAME


def space_key() -> str:
    """Identity of the embedding space — vectors are only comparable within one."""
    return "local:" + _MODEL_NAME


def _device() -> str:
    """``$THREAD_ARCHIVE_EMBED_DEVICE`` if set, else the best accelerator (Apple
    ``mps`` / CUDA), falling back to ``cpu``. On Apple silicon the GPU is ~20× the
    CPU — the difference between an hour and a day for the corpus embed."""
    dev = os.environ.get("THREAD_ARCHIVE_EMBED_DEVICE")
    if dev:
        return dev
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def is_available() -> bool:
    """True when sentence-transformers is importable (the extra is installed). Cheap —
    does not load the model; a load failure degrades at call time."""
    try:
        if importlib.util.find_spec("sentence_transformers") is None:
            return False
    except ImportError:
        return False
    return not _load_failed


def _hub_cache_dir() -> str:
    """The HF hub cache directory, resolved the way huggingface_hub does — but as a pure
    filesystem path, with no `huggingface_hub` import. Called *before* we set the offline
    env, so it must not pull in the hub (which freezes ``HF_HUB_OFFLINE`` at import)."""
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return os.environ["HUGGINGFACE_HUB_CACHE"]
    if os.environ.get("HF_HOME"):
        return os.path.join(os.environ["HF_HOME"], "hub")
    return os.path.expanduser("~/.cache/huggingface/hub")


def _model_cached() -> bool:
    """True when ``_MODEL_NAME`` is already in the HF hub cache (a non-empty ``snapshots/``).
    Pure filesystem probe — no hub import — so it's safe to consult before going offline."""
    folder = "models--" + _MODEL_NAME.replace("/", "--")
    snaps = os.path.join(_hub_cache_dir(), folder, "snapshots")
    try:
        return os.path.isdir(snaps) and any(os.scandir(snaps))
    except OSError:
        return False


def _pin_offline_if_cached() -> None:
    """Pin the model load to the local cache once it's downloaded, so the hot path makes
    NO Hub request: no revision re-resolve, no ``trust_remote_code`` re-fetch, no
    unauthenticated-HF-Hub warning, and it still works with the network down. The model is
    pinned (one fixed ``_MODEL_NAME``) — a Hub round-trip on every search buys nothing.

    Skipped when the model isn't cached yet (so a first install still downloads it) or when
    ``THREAD_ARCHIVE_EMBED_ONLINE=1`` forces an online load (first download / deliberate
    refresh). ``huggingface_hub`` freezes ``HF_HUB_OFFLINE`` into a module constant at
    import, so set the env *before* it's imported and, if it already is, flip the live
    constant too."""
    if os.environ.get("THREAD_ARCHIVE_EMBED_ONLINE") == "1" or not _model_cached():
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    const = sys.modules.get("huggingface_hub.constants")
    if const is not None:
        const.HF_HUB_OFFLINE = True


def _load():
    """Lazily construct the cached SentenceTransformer (nomic needs trust_remote_code)."""
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    try:
        _pin_offline_if_cached()
        from sentence_transformers import SentenceTransformer

        device = _device()
        _model = SentenceTransformer(_MODEL_NAME, trust_remote_code=True, device=device)
        dim = getattr(_model, "get_embedding_dimension", _model.get_sentence_embedding_dimension)()
        logger.info("embed: loaded %s (dim=%s, device=%s)", _MODEL_NAME, dim, device)
        return _model
    except Exception as e:  # noqa: BLE001
        _load_failed = True
        logger.warning("embed: model load failed (%s) — vector arm degrades", e)
        return None


def _cap(text: str) -> str:
    return text[:EMBEDDING_CHAR_CAP]


def _encode(prefixed: list[str]):
    model = _load()
    if model is None:
        return None
    try:
        # Un-normalized to match the contract — the vector store normalizes on write.
        vecs = model.encode(prefixed, normalize_embeddings=False, convert_to_numpy=True)
        return [v.astype("float32").tolist() for v in vecs]
    except Exception as e:  # noqa: BLE001
        logger.warning("embed: encode failed (%s)", e)
        return None


def embed_query(text: str):
    """Embed a search query (nomic ``search_query:`` prefix). None on any failure."""
    if not text or not text.strip():
        return None
    vecs = _encode([f"search_query: {_cap(text)}"])
    return vecs[0] if vecs else None


def embed_documents(texts: list[str]):
    """Embed indexed content (nomic ``search_document:`` prefix), in input order. None
    on any failure (the caller writes nothing — never a partial/wrong-space batch)."""
    if not texts:
        return None
    return _encode([f"search_document: {_cap(t)}" for t in texts])
