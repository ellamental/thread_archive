"""In-process cross-encoder re-rank — a bge-reranker via sentence-transformers.

The in-process cross-encoder: model ``bge-reranker-v2-m3``, ``(query, doc)`` joint
scoring, fail-soft contract — run **in-process** through sentence-transformers'
``CrossEncoder`` rather than as an HTTP client to a llama-server, the same way
:mod:`.embed` runs the embedding model in-process. No daemon, no second process:
the model loads in this process, gated to the ``[embeddings]`` extra (torch).

Why it exists: the bi-encoder ANN (the vector arm) puts the true target in the
top-20 often but at rank 1 rarely — semantic look-alikes outrank it, and a
vocab-mismatch target has lexical density ~0 so the lexical scorer can't separate
them either. A cross-encoder scores (query, candidate) *jointly* and pulls the
target up the mid-list (found@1 0.21→0.285, MRR 0.37→0.45 — measured on the
title-proxy eval, which flatters every layer; on log-mined real queries the
pipeline-level lift is ~2 points of recall@10 for 5× the latency, which is why
the auto-gate confines the re-rank to the vocab-mismatch queries it exists for).

**Fail-soft by contract.** Every entry point returns ``None`` (caller keeps its
order) on any error — a missing extra, a failed model load, an encode error. The
re-rank is a precision booster on the head, never a correctness dependency: with
no ``[embeddings]`` extra the whole stage degrades out and search stays
lexical+vector. ``is_available()`` is cheap (does not load the model); a load
failure degrades at call time and is cached so it costs one attempt, not one per
query.
"""

from __future__ import annotations

import importlib.util
import logging
import os

from .model_slot import ModelSlot

logger = logging.getLogger(__name__)

# Doc head fed to the cross-encoder. The relevance signal lives in the opening of a
# message; capping bounds each (query, doc) pair well under the model ctx and keeps
# latency sane (~1500 chars ≈ ~375 tokens).
RERANK_DOC_CHARS = 1500
_MODEL_NAME = os.environ.get("THREAD_ARCHIVE_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

# Inference batch size for predict(). Latency here is padding-bound: one big batch
# pads every (query, doc) pair to the batch's longest doc, so a single long hit
# makes the whole pool pay its length. Small batches bound the padding waste —
# measured on MPS over real 24-doc pools, 8 beats 32 by ~2× at identical scores.
_PREDICT_BATCH_SIZE = 8

# The lazily-loaded ~570M cross-encoder + degrade flag — the public state seam
# (see ModelSlot).
SLOT = ModelSlot()


def model_name() -> str:
    return _MODEL_NAME


def _device() -> str:
    """``$THREAD_ARCHIVE_RERANK_DEVICE`` (else the shared embed device) if set, else
    the best accelerator (Apple ``mps`` / CUDA), falling back to ``cpu``."""
    dev = os.environ.get("THREAD_ARCHIVE_RERANK_DEVICE") or os.environ.get("THREAD_ARCHIVE_EMBED_DEVICE")
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
    """True when sentence-transformers is importable (the ``[embeddings]`` extra is
    installed). Cheap — does not load the model; a load failure degrades at call time."""
    try:
        if importlib.util.find_spec("sentence_transformers") is None:
            return False
    except ImportError:
        return False
    return not SLOT.load_failed


def _construct():
    """Construct the CrossEncoder (fp16 on an accelerator, fp32 on CPU).

    Raises on any failure — including the ``[embeddings]`` extra being absent —
    and the slot catches it, caching the failure so the re-rank stage degrades."""
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as e:  # extra absent — degrade via the slot's failure cache
        raise RuntimeError(f"[embeddings] extra not installed: {e}") from e

    device = _device()
    model_kwargs = {}
    if device.startswith(("mps", "cuda")):
        # fp16 on an accelerator: ~2× the inference speed at scores whose
        # ordering is indistinguishable from fp32 (measured over real
        # pools: zero pairwise rank flips). CPU stays fp32 — fp16 there
        # is emulated and slower.
        try:
            import torch
        except ImportError as e:  # extra absent — degrade via the slot's failure cache
            raise RuntimeError(f"[embeddings] extra not installed: {e}") from e

        model_kwargs["torch_dtype"] = torch.float16
    model = CrossEncoder(_MODEL_NAME, device=device, model_kwargs=model_kwargs)
    logger.info("rerank: loaded %s (device=%s)", _MODEL_NAME, device)
    return model


def _load():
    """The cached CrossEncoder, loading it on first call. None on failure —
    cached by the slot, so it costs one attempt, not one per query."""
    return SLOT.get(_construct, lambda e: logger.warning(
        "rerank: model load failed (%s) — re-rank stage degrades", e))


def warm() -> bool:
    """Eagerly load the cross-encoder so it isn't cold-loaded inside the first conceptual
    query (a >60s stall that can blow past an MCP client's request timeout). Fail-soft and
    idempotent: returns False when the extra is absent or the load fails."""
    if not is_available():
        return False
    return _load() is not None


def rerank_scores(query: str, docs: list[str]) -> list[float] | None:
    """Relevance score per doc (in input order), or ``None`` on any failure.

    Length-capped and fail-soft. A ``None`` return means "reranker unavailable /
    errored — keep the caller's order." Pure: no I/O beyond the in-process model."""
    if not query or not docs:
        return None
    model = _load()
    if model is None:
        return None
    try:
        pairs = [[query, (d or "")[:RERANK_DOC_CHARS]] for d in docs]
        scores = model.predict(pairs, batch_size=_PREDICT_BATCH_SIZE, show_progress_bar=False)
        return [float(s) for s in scores]
    except Exception as e:  # noqa: BLE001
        logger.debug("rerank failed (%s) — caller keeps original order", e)
        return None


def rerank(query: str, items: list, get_text) -> list | None:
    """Reorder ``items`` by cross-encoder relevance to ``query``. ``get_text(item)``
    yields the text to score. Returns a new list (best-first), or ``None`` if the
    reranker is unavailable/errored (caller keeps its order). Pure helper."""
    if not items:
        return None
    scores = rerank_scores(query, [get_text(it) for it in items])
    if scores is None:
        return None
    return [it for _, it in sorted(zip(scores, items), key=lambda p: -p[0])]
