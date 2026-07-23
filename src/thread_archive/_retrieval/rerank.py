"""In-process cross-encoder re-rank — a bge-reranker via sentence-transformers.

The in-process cross-encoder: model ``bge-reranker-v2-m3``, ``(query, doc)`` joint
scoring, fail-soft contract — run **in-process** through sentence-transformers'
``CrossEncoder`` rather than as an HTTP client to a llama-server, the same way
:mod:`.embed` runs the embedding model in-process. No daemon, no second process:
the model loads in this process, gated to the ``[embeddings]`` extra (torch).

Why it exists: the bi-encoder ANN (the vector arm) puts the true target in the
top-20 often but at rank 1 rarely — semantic look-alikes outrank it, and a
vocab-mismatch target has lexical density ~0 so the lexical scorer can't separate
them either. A cross-encoder scores (query, candidate) *jointly* to separate the
true target from its semantic look-alikes — a signal neither the bi-encoder nor
the lexical scorer computes. It costs ~5× the latency of the arms it re-ranks,
which is why the auto-gate confines the re-rank to the vocab-mismatch queries it
exists for.

A :class:`Reranker` owns one cross-encoder plus the pairing policy. The
module-level functions delegate to the process default (:func:`default`); the
search pipeline takes a ``reranker`` argument, so a caller with its own — a
lent model, a different cross-encoder — passes it in rather than replacing the
default.

**Fail-soft by contract.** Every entry point returns ``None`` (caller keeps its
order) on any error — a missing extra, a failed model load, an encode error. The
re-rank is a precision booster on the head, never a correctness dependency: with
no ``[embeddings]`` extra the whole stage degrades out and search stays
lexical+vector. ``is_available()`` is cheap (does not load the model); a load
failure degrades at call time and is cached so it costs one attempt, not one per
query. ``$THREAD_ARCHIVE_RERANK=off`` stands the stage down outright, the way
``$THREAD_ARCHIVE_EMBED=off`` does the vector arm.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, Optional, Protocol

from .embed import importable, models_enabled, select_device, torch_accelerators
from .model_slot import ModelSlot

logger = logging.getLogger(__name__)


class PairScorer(Protocol):
    """All a reranker needs of a loaded model: score (query, doc) pairs jointly. A
    sentence-transformers ``CrossEncoder`` satisfies it."""

    def predict(self, sentences: list[list[str]], batch_size: int,
                show_progress_bar: bool) -> Any: ...


# Doc head fed to the cross-encoder. The relevance signal lives in the opening of a
# message; capping bounds each (query, doc) pair well under the model ctx and keeps
# latency sane (~1500 chars ≈ ~375 tokens).
RERANK_DOC_CHARS = 1500

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"

# Inference batch size for predict(). Latency here is padding-bound: one big batch
# pads every (query, doc) pair to the batch's longest doc, so a single long hit
# makes the whole pool pay its length. Small batches bound the padding waste —
# measured on MPS over real 24-doc pools, 8 beats 32 by ~2× at identical scores.
_PREDICT_BATCH_SIZE = 8


def model_name() -> str:
    """The configured cross-encoder: ``$THREAD_ARCHIVE_RERANK_MODEL`` or the default."""
    return os.environ.get("THREAD_ARCHIVE_RERANK_MODEL") or DEFAULT_MODEL


def _device() -> str:
    """``$THREAD_ARCHIVE_RERANK_DEVICE`` (else the shared embed device) if set, else
    the best accelerator (Apple ``mps`` / CUDA), falling back to ``cpu``."""
    override = (os.environ.get("THREAD_ARCHIVE_RERANK_DEVICE")
                or os.environ.get("THREAD_ARCHIVE_EMBED_DEVICE"))
    return select_device(override, *torch_accelerators())


def dtype_kwargs(device: str) -> dict:
    """Model kwargs for ``device``: fp16 on an accelerator — ~2× the inference speed at
    scores whose ordering is indistinguishable from fp32 (measured over real pools: zero
    pairwise rank flips) — and nothing on CPU, where fp16 is emulated and slower."""
    if not device.startswith(("mps", "cuda")):
        return {}
    try:
        import torch
    except ImportError as e:  # extra absent — degrade via the slot's failure cache
        raise RuntimeError(f"[embeddings] extra not installed: {e}") from e
    return {"torch_dtype": torch.float16}


def build_model(cross_encoder: Callable[..., PairScorer], name: str) -> PairScorer:
    """Instantiate ``cross_encoder`` for ``name`` on the chosen device, at the dtype that
    device wants. Split from the import so the construction policy is one readable call."""
    device = _device()
    model = cross_encoder(name, device=device, model_kwargs=dtype_kwargs(device))
    logger.info("rerank: loaded %s (device=%s)", name, device)
    return model


class Reranker:
    """One cross-encoder plus the pairing policy the archive scores with: the doc-head
    cap and the inference batch size.

    Same lazy, fail-soft contract as :class:`~.embed.Embedder` — the model loads on
    first score, a failed load is cached, and every entry point returns ``None`` so
    the caller keeps its own order. Pass ``model=`` to use an already-loaded
    CrossEncoder or ``load=`` to supply a different loader, which then answers for
    its own dependencies rather than the ``[embeddings]`` extra.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        *,
        model: Optional[PairScorer] = None,
        load: Optional[Callable[[], PairScorer]] = None,
    ) -> None:
        self._name = name
        self._needs_extra = load is None
        self._slot: ModelSlot[PairScorer] = ModelSlot(
            load or self._construct, self._degrade, model=model)

    @property
    def name(self) -> str:
        """The cross-encoder this reranker loads: the one it was constructed with, else
        the configured one, resolved per call rather than frozen at import."""
        return self._name or model_name()

    def _degrade(self, e: Exception) -> None:
        logger.warning("rerank: model load failed (%s) — re-rank stage degrades", e)

    def _construct(self) -> PairScorer:
        """Construct the CrossEncoder.

        Raises on any failure — including the ``[embeddings]`` extra being absent —
        and the slot catches it, caching the failure so the re-rank stage degrades."""
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:  # extra absent — degrade via the slot's failure cache
            raise RuntimeError(f"[embeddings] extra not installed: {e}") from e
        return build_model(CrossEncoder, self.name)

    def is_available(self) -> bool:
        """True when this reranker can score: models aren't switched off, the model is
        loaded or loadable, and no load has already failed. Cheap — never loads the
        model; a load failure degrades at call time."""
        if not models_enabled("THREAD_ARCHIVE_RERANK"):
            return False
        if self._slot.model is not None:
            return True
        if self._needs_extra and not importable("sentence_transformers"):
            return False
        return not self._slot.load_failed

    def is_loaded(self) -> bool:
        """True when the cross-encoder is already resident — a query would pay no
        load. The cold/warm signal the usage ledger records: an available-but-unloaded
        reranker means the first conceptual query eats the (tens-of-seconds) load."""
        return self._slot.model is not None

    def warm(self) -> bool:
        """Eagerly load the cross-encoder so it isn't cold-loaded inside the first
        conceptual query (a >60s stall that can blow past an MCP client's request
        timeout). Fail-soft and idempotent: returns False when it's unavailable or the
        load fails."""
        if not self.is_available():
            return False
        return self._slot.get() is not None

    def rerank_scores(self, query: str, docs: list[str]) -> Optional[list[float]]:
        """Relevance score per doc (in input order), or ``None`` on any failure.

        Length-capped and fail-soft. A ``None`` return means "reranker unavailable /
        errored — keep the caller's order." Pure: no I/O beyond the in-process model."""
        if not query or not docs:
            return None
        # The degrade contract, checked here and not only at the pipeline's gate, so a
        # caller that reaches scoring directly honors it too and a switched-off stage
        # can never cold-load the cross-encoder.
        if not self.is_available():
            return None
        pairs = [[query, (d or "")[:RERANK_DOC_CHARS]] for d in docs]
        try:
            with self._slot.use() as model:
                if model is None:
                    return None
                scores = model.predict(pairs, batch_size=_PREDICT_BATCH_SIZE, show_progress_bar=False)
        except Exception as e:  # noqa: BLE001
            logger.debug("rerank failed (%s) — caller keeps original order", e)
            return None
        return [float(s) for s in scores]

    def rerank(self, query: str, items: list, get_text) -> Optional[list]:
        """Reorder ``items`` by cross-encoder relevance to ``query``. ``get_text(item)``
        yields the text to score — a single string, or several passages of one hit,
        in which case the hit scores as its best passage (MaxP: a long doc's
        answering passage counts even when its head doesn't). Returns a new list
        (best-first), or ``None`` if the reranker is unavailable/errored (caller
        keeps its order). Stable on ties. Pure helper."""
        if not items:
            return None
        flat: list[str] = []
        spans: list[list[int]] = []
        for it in items:
            texts = get_text(it)
            if isinstance(texts, str):
                texts = [texts]
            spans.append([len(flat) + i for i in range(len(texts))])
            flat.extend(texts)
        scores = self.rerank_scores(query, flat)
        if scores is None:
            return None
        item_score = [max((scores[i] for i in idxs), default=float("-inf")) for idxs in spans]
        order = sorted(range(len(items)), key=lambda i: -item_score[i])
        return [items[i] for i in order]


# The process reranker: one cross-encoder shared by every caller without its own.
_DEFAULT = Reranker()


def default() -> Reranker:
    """The process reranker — the one the search pipeline falls back to."""
    return _DEFAULT


def is_available() -> bool:
    """True when the process reranker can score. Cheap — does not load the model."""
    return _DEFAULT.is_available()


def is_loaded() -> bool:
    """True when the process reranker is already resident (a query pays no load)."""
    return _DEFAULT.is_loaded()


def warm() -> bool:
    """Preload the process reranker. False when it's unavailable or the load fails."""
    return _DEFAULT.warm()


def rerank_scores(query: str, docs: list[str]) -> Optional[list[float]]:
    """Score ``docs`` against ``query`` with the process reranker. None on any failure."""
    return _DEFAULT.rerank_scores(query, docs)


def rerank(query: str, items: list, get_text) -> Optional[list]:
    """Reorder ``items`` with the process reranker. None when it's unavailable."""
    return _DEFAULT.rerank(query, items, get_text)
