"""Voyage AI embeddings for transaction search (brief §5).

Shared embedding space with reference-demo: `voyage-4-large` indexes documents,
`voyage-4-lite` embeds queries — same space, so doc and query vectors are
directly comparable. Ported from reference-demo/backend/services/embedding_service.py.

`voyageai` is imported lazily inside `_get_client()` so the API process can
import this module even when the optional dependency / key isn't present —
search just degrades (the service falls back to text-only).
"""

from __future__ import annotations

from acme_ofv.config import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        import voyageai  # lazy — keep the module importable without the dep
        _client = voyageai.Client(api_key=settings().voyage_api_key)
    return _client


def embed_for_index(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed texts for indexing using voyage-4-large."""
    result = _get_client().embed(
        texts, model=settings().voyage_index_model, input_type=input_type)
    return result.embeddings


def embed_for_query(text: str) -> list[float]:
    """Embed a query using voyage-4-lite (shared space with voyage-4-large)."""
    result = _get_client().embed(
        [text], model=settings().voyage_query_model, input_type="query")
    return result.embeddings[0]


def embed_batch(texts: list[str], batch_size: int = 128,
                input_type: str = "document") -> list[list[float]]:
    """Embed large batches by splitting into chunks."""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        out.extend(embed_for_index(texts[i:i + batch_size], input_type=input_type))
        print(f"    embedded {min(i + batch_size, len(texts))}/{len(texts)}", flush=True)
    return out


def rerank(query: str, documents: list[str], top_k: int = 10) -> list[dict]:
    """Optional final re-rank with Voyage rerank-2.5."""
    result = _get_client().rerank(
        query=query, documents=documents,
        model=settings().voyage_rerank_model, top_k=top_k)
    return [{"index": r.index, "relevance_score": r.relevance_score} for r in result.results]
