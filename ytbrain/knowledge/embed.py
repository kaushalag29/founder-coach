"""Local embedding and reranking models, loaded lazily and once per process.

sentence-transformers comes with the optional `index` extra; everything that
imports it goes through here so a missing extra produces one clear message.
"""
from __future__ import annotations

import os

from typing import Callable, Protocol

from ..config import EMBED_MAX_SEQ, EMBED_MODEL, RERANK_MODEL, TORCH_DEVICE

MISSING_EXTRA = ('the knowledge index needs the optional "index" extra: '
                 'uv pip install -e ".[index]"')


class Embedder(Protocol):
    name: str
    def __call__(self, texts: list[str]) -> list[list[float]]: ...


DEVICES = ("auto", "mps", "cpu", "cuda")


def resolve_device(requested: str | None = None) -> str:
    """The PyTorch device to use. `auto` prefers the Apple-silicon GPU (mps), then
    cuda, then cpu; an explicit device that isn't available is an error, not a
    silent fallback, so a slow CPU run is never a surprise."""
    want = (requested or TORCH_DEVICE or "auto").lower()
    if want not in DEVICES:
        raise RuntimeError(f"unknown device '{want}' (choose from {', '.join(DEVICES)})")
    if want in ("auto", "mps"):
        # ops MPS lacks run on the CPU instead of crashing the run
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch
    mps = torch.backends.mps.is_available()
    cuda = torch.cuda.is_available()
    if want == "auto":
        return "mps" if mps else "cuda" if cuda else "cpu"
    if want == "mps" and not mps:
        raise RuntimeError("device 'mps' requested but not available (needs Apple silicon, "
                           "macOS 12.3+ and the standard macOS arm64 PyTorch wheel); use --device cpu")
    if want == "cuda" and not cuda:
        raise RuntimeError("device 'cuda' requested but no CUDA GPU is available; use --device cpu")
    return want


def _quiet_hub() -> None:
    """Stop transformers from fetching a converted .safetensors copy in a background thread.

    Repos that ship only pytorch_model.bin (BAAI/bge-m3) make transformers start a
    non-daemon "auto_conversion" thread that downloads a second 2+ GB copy of the
    weights from a Hub PR ref -- competing with the index run for bandwidth and keeping
    the process alive after `ytbrain index` has finished. The .bin loads fine.
    Set DISABLE_SAFETENSORS_CONVERSION=0 to allow it.
    """
    os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")


class SentenceTransformerEmbedder:
    def __init__(self, model: str = EMBED_MODEL, device: str | None = None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(MISSING_EXTRA) from e
        self.name = model
        self.device = resolve_device(device)
        _quiet_hub()
        self._m = SentenceTransformer(model, device=self.device)
        self._m.max_seq_length = min(EMBED_MAX_SEQ, self._m.max_seq_length or EMBED_MAX_SEQ)
        dim = getattr(self._m, "get_embedding_dimension", None) or self._m.get_sentence_embedding_dimension
        self.dim = dim()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vecs = self._m.encode(texts, batch_size=16, normalize_embeddings=True,
                              show_progress_bar=False, convert_to_numpy=True)
        return vecs.tolist()


def load_embedder(model: str = EMBED_MODEL, device: str | None = None) -> Embedder:
    return SentenceTransformerEmbedder(model, device)


Reranker = Callable[[str, list[str]], list[float]]


def load_reranker(model: str = RERANK_MODEL, device: str | None = None) -> Reranker | None:
    """Cross-encoder scores in 0..1, or None when disabled/unavailable."""
    if not model or model.lower() == "none":
        return None
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as e:
        raise RuntimeError(MISSING_EXTRA) from e
    _quiet_hub()
    ce = CrossEncoder(model, device=resolve_device(device), max_length=EMBED_MAX_SEQ)

    def score(query: str, texts: list[str]) -> list[float]:
        import math
        raw = ce.predict([(query, t) for t in texts], batch_size=16, show_progress_bar=False)
        out = []
        for x in raw:
            x = float(x)
            out.append(x if 0.0 <= x <= 1.0 else 1 / (1 + math.exp(-x)))   # logits -> 0..1
        return out
    return score
