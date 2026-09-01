"""Local ONNX embeddings via fastembed (NFR-3: embeddings never leave the LAN).

fastembed is synchronous CPU ONNX — all calls go through asyncio.to_thread.
The model loads lazily on first use and is process-wide singleton.
"""

from __future__ import annotations

import asyncio
import threading

from .config import get_config
from .log import get_logger

log = get_logger(__name__)
_lock = threading.Lock()
_model = None
_failed = False


def _resolve_embed_model(text_embedding_cls) -> str:
    """fastembed 0.8 dropped BAAI/bge-m3 from TextEmbedding; keep 1024-d fallback."""
    requested = get_config().embed_model
    supported = {m.get("model") for m in text_embedding_cls.list_supported_models()}
    if requested in supported:
        return requested
    fallback = "BAAI/bge-large-en-v1.5"
    if get_config().embed_dim == 1024 and fallback in supported:
        log.warning(
            "embed model %s is not a TextEmbedding model; falling back to %s",
            requested, fallback,
        )
        return fallback
    raise ValueError(f"embed model {requested!r} is not supported by TextEmbedding")


def _get_model():
    global _model, _failed
    if _model is None and not _failed:
        try:
            from fastembed import TextEmbedding

            name = _resolve_embed_model(TextEmbedding)
            _model = TextEmbedding(model_name=name)
            log.info("embed model loaded", extra={"err": name})
        except Exception:
            _failed = True
            log.exception("embed model failed to load — vector search degraded, FTS still works")
    return _model


def embed_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    if model is None:
        return [[0.0] * get_config().embed_dim] * len(texts)
    return [list(v) for v in model.embed(texts)]


async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return await asyncio.to_thread(embed_sync, texts)


async def embed_one(text: str) -> list[float]:
    vecs = await embed([text])
    return vecs[0]


# ── optional cross-encoder rerank (FR-6 P1, off by default) ─────────────────

_reranker = None
_reranker_failed = False


def _get_reranker():
    global _reranker, _reranker_failed
    if _reranker is None and not _reranker_failed:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            _reranker = TextCrossEncoder(model_name=get_config().rerank_model)
        except Exception:
            _reranker_failed = True
            log.exception("reranker failed to load; rerank mode disabled")
    return _reranker


def rerank_sync(query: str, texts: list[str]) -> list[float]:
    r = _get_reranker()
    if r is None:
        return [0.0] * len(texts)
    return [float(x) for x in r.rerank([(query, t) for t in texts])]


async def rerank_scores(query: str, texts: list[str]) -> list[float]:
    if not texts:
        return []
    return await asyncio.to_thread(rerank_sync, query, texts)
