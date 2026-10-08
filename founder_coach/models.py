"""ONNX query models for the coach runtime (no torch): an embedder and an optional
cross-encoder reranker, both through fastembed. Models download once, on first use,
to the models folder (FOUNDER_COACH_MODELS, else ~/.founder-coach/models).

The pack records which embedding model and prefixes built its vectors; the runtime
reads them from the pack, so queries and items are always embedded the same way.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from . import product

DEFAULT_EMBED_MODEL = "BAAI/bge-base-en-v1.5"            # 0.21 GB ONNX, 768 dims, MIT
DEFAULT_RERANK_MODEL = "jinaai/jina-reranker-v1-turbo-en"  # 0.15 GB ONNX, Apache-2.0

_RETRIEVAL = "Represent this sentence for searching relevant passages: "
# model -> (query prefix, document prefix), as each model card recommends for retrieval
PREFIXES: dict[str, tuple[str, str]] = {
    "BAAI/bge-small-en-v1.5": (_RETRIEVAL, ""),
    "BAAI/bge-base-en-v1.5": (_RETRIEVAL, ""),
    "BAAI/bge-large-en-v1.5": (_RETRIEVAL, ""),
    "snowflake/snowflake-arctic-embed-xs": (_RETRIEVAL, ""),
    "snowflake/snowflake-arctic-embed-s": (_RETRIEVAL, ""),
    "snowflake/snowflake-arctic-embed-m": (_RETRIEVAL, ""),
    "snowflake/snowflake-arctic-embed-l": (_RETRIEVAL, ""),
    "mixedbread-ai/mxbai-embed-large-v1": (_RETRIEVAL, ""),
    "nomic-ai/nomic-embed-text-v1.5": ("search_query: ", "search_document: "),
    "nomic-ai/nomic-embed-text-v1.5-Q": ("search_query: ", "search_document: "),
}

MISSING_EXTRA = ('the Knowledge pack needs the optional "pack" extra: '
                 'uv pip install -e ".[pack]"')


def home() -> Path:
    return Path(product.env("HOME") or product.default_home()).expanduser()


def models_dir() -> Path:
    """<PREFIX>MODELS; else <PREFIX>HOME/models when the data folder is pinned; else the engine home's models/
    (~/.ytbrain/models, shared by every coach), reading the old ~/.<id>/models until that exists, so an
    update never downloads the models again."""
    explicit = product.env("MODELS")
    if explicit:
        p = Path(explicit).expanduser()
    elif product.env("HOME"):
        p = home() / "models"
    else:
        from .installed import engine_home
        p = engine_home() / "models"
        old = product.default_home() / "models"
        if not p.is_dir() and old.is_dir() and any(old.iterdir()):
            return old
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fastembed():
    try:
        import fastembed
    except ImportError as e:
        raise RuntimeError(MISSING_EXTRA) from e
    return fastembed


def onnx_providers(device: str | None) -> list[str] | None:
    """ONNX Runtime execution providers for `device`: None/"cpu" -> the default (CPU);
    "coreml" -> Apple's CoreML (GPU / Neural Engine) with CPU fallback for unsupported ops;
    "cuda" -> CUDA with CPU fallback. An unavailable provider is an error, not a silent
    fallback, so a slow run is never a surprise."""
    if not device or device == "cpu":
        return None
    want = {"coreml": "CoreMLExecutionProvider", "cuda": "CUDAExecutionProvider"}.get(device)
    if want is None:
        raise RuntimeError(f"unknown ONNX device '{device}' (choose cpu, coreml or cuda)")
    try:
        import onnxruntime
        have = onnxruntime.get_available_providers()
    except Exception as e:            # noqa: BLE001 -- report whatever went wrong
        raise RuntimeError(f"can't query ONNX Runtime providers: {e}") from e
    if want not in have:
        raise RuntimeError(f"{want} isn't available in this onnxruntime build (has: {', '.join(have)}); "
                           f"use --device cpu")
    return [want, "CPUExecutionProvider"]


def _load(kind: str, cls, model: str, threads: int | None, providers: list[str] | None = None):
    try:
        if providers:
            return cls(model, cache_dir=str(models_dir()), threads=threads, providers=providers)
        return cls(model, cache_dir=str(models_dir()), threads=threads)
    except Exception as e:              # unknown model, no network on first use, bad cache
        raise RuntimeError(f"could not load the {kind} model {model}: {e} (first use downloads "
                           f"it to {models_dir()})") from e


class OnnxEmbedder:
    """`embedder(texts)` embeds queries; `embedder.documents(texts)` embeds items.
    Both return L2-normalised float32 rows."""

    def __init__(self, model: str = DEFAULT_EMBED_MODEL, query_prefix: str | None = None,
                 doc_prefix: str | None = None, threads: int | None = None, batch_size: int = 32,
                 device: str | None = None):
        fe = _fastembed()
        providers = onnx_providers(device)
        self.device = device or "cpu"
        qp, dp = PREFIXES.get(model, ("", ""))
        self.name = model
        self.query_prefix = qp if query_prefix is None else query_prefix
        self.doc_prefix = dp if doc_prefix is None else doc_prefix
        self.batch_size = batch_size
        self._m = _load("embedding", fe.TextEmbedding, model, threads, providers)
        self.dim = int(self._embed(["dimension probe"]).shape[1])

    def _embed(self, texts: list[str]):
        import numpy as np
        if not texts:
            return np.zeros((0, getattr(self, "dim", 0)), dtype=np.float32)
        arr = np.asarray(list(self._m.embed(list(texts), batch_size=self.batch_size)),
                         dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1.0, norms)

    def __call__(self, texts: list[str]):
        return list(self._embed([self.query_prefix + t for t in texts]))

    def documents(self, texts: list[str]):
        return self._embed([self.doc_prefix + t for t in texts])


class OnnxReranker:
    """(query, texts) -> relevance in 0..1 (the cross-encoder's logit through a sigmoid)."""

    def __init__(self, model: str = DEFAULT_RERANK_MODEL, threads: int | None = None,
                 batch_size: int = 16):
        from importlib import import_module
        _fastembed()
        try:
            cls = import_module("fastembed.rerank.cross_encoder").TextCrossEncoder
        except (ImportError, AttributeError) as e:
            raise RuntimeError(MISSING_EXTRA) from e
        self.name = model
        self.batch_size = batch_size
        self._m = _load("reranker", cls, model, threads)

    def __call__(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        raw = self._m.rerank(query, list(texts), batch_size=self.batch_size)
        return [1.0 / (1.0 + math.exp(-float(x))) for x in raw]


def load_embedder(model: str = DEFAULT_EMBED_MODEL, query_prefix: str | None = None,
                  doc_prefix: str | None = None, device: str | None = None) -> OnnxEmbedder:
    return OnnxEmbedder(model, query_prefix, doc_prefix, device=device)


def load_reranker(model: str | None = DEFAULT_RERANK_MODEL) -> OnnxReranker | None:
    if not model or model.lower() == "none":
        return None
    return OnnxReranker(model)


def for_pack(meta: dict, rerank: bool = True) -> tuple[OnnxEmbedder, OnnxReranker | None]:
    """The query models a pack was built for, with its recorded prefixes."""
    embed = load_embedder(meta["embed_model"], meta.get("query_prefix", ""), meta.get("doc_prefix", ""))
    return embed, (load_reranker(meta.get("rerank_model")) if rerank else None)
