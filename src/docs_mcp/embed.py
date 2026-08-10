"""Local CPU embeddings via fastembed (ONNX, no PyTorch).

Models are loaded lazily and cached per process. Both are baked into the image at
build time, so nothing is downloaded at runtime.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

import numpy as np

from .config import settings

# BGE retrieval models are trained asymmetrically: queries carry this instruction,
# passages do not. Applying it to both (or neither) measurably degrades recall.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

_lock = threading.Lock()
_dense = None
_reranker = None
_dim: int | None = None


def _kwargs() -> dict:
    return {"threads": settings.threads} if settings.threads else {}


def dense_model():
    global _dense
    if _dense is None:
        with _lock:
            if _dense is None:
                from fastembed import TextEmbedding

                _dense = TextEmbedding(model_name=settings.dense_model, **_kwargs())
    return _dense


def reranker():
    global _reranker
    if _reranker is None:
        with _lock:
            if _reranker is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                _reranker = TextCrossEncoder(
                    model_name=settings.rerank_model, **_kwargs()
                )
    return _reranker


def dimension() -> int:
    """Embedding width, measured from the model rather than hardcoded."""
    global _dim
    if _dim is None:
        _dim = len(next(iter(dense_model().embed(["probe"]))))
    return _dim


def embed_passages(texts: Iterable[str]) -> list[np.ndarray]:
    return [
        np.asarray(v, dtype=np.float32)
        for v in dense_model().embed(list(texts), batch_size=settings.embed_batch)
    ]


def embed_query(query: str) -> np.ndarray:
    vector = next(iter(dense_model().embed([QUERY_INSTRUCTION + query])))
    return np.asarray(vector, dtype=np.float32)


def rerank(query: str, documents: list[str]) -> list[float]:
    return list(reranker().rerank(query, documents))


def warmup() -> None:
    """Force both models onto disk and through one inference pass.

    Both are fetched regardless of the RERANK setting: this runs at image build
    time, and the runtime container has no network (HF_HUB_OFFLINE=1), so baking
    the reranker now is what makes RERANK=1 a working runtime toggle later.
    """
    print(f"dense    : {settings.dense_model} (dim={dimension()})")
    rerank("probe", ["probe document"])
    print(f"reranker : {settings.rerank_model}")
