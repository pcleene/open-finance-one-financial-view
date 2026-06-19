"""Transaction search orchestration (brief §5) — assembles the builders, runs
them, and owns the fallback chain. Consent scope is injected by the caller
(the gate's allowed-account set), applied to BOTH legs.

Fallback order (per /add-hybrid-search):
  $rankFusion  →  $vectorSearch-only  →  $search-only  →  application-side RRF.
Covers pre-8.1 clusters and customers with no embeddings (text always returns).
"""

from __future__ import annotations

import logging

from acme_ofv.config import settings
from acme_ofv.db import aggregate_list
from acme_ofv.search.pipeline_builders import (
    build_hybrid_pipeline,
    build_text_only_pipeline,
    build_text_stage,
    build_vector_only_pipeline,
    build_vector_stage,
    consent_search_filter,
    consent_vector_prefilter,
    rrf_merge,
)

logger = logging.getLogger(__name__)


class SearchService:
    """Constructed with the db (DIP); no global client access."""

    def __init__(self, db):
        self.db = db

    async def _run(self, pipeline: list[dict]) -> list[dict]:
        # via aggregate_list so the leg is captured by the Query Inspector
        return await aggregate_list(self.db.transactions, pipeline)

    async def _has_vectors(self, customer_id: str) -> bool:
        doc = await self.db.transactions.find_one(
            {"customer_id": customer_id, "embedding": {"$exists": True}}, {"_id": 1})
        return doc is not None

    def _query_vector(self, q: str):
        """Embed the query with voyage-4-lite — degrade to None on any failure."""
        if not settings().voyage_api_key:
            return None
        try:
            from acme_ofv.search.embedding_service import embed_for_query
            return embed_for_query(q)
        except Exception as exc:  # missing dep / API error — fall back to text
            logger.warning("query embedding failed: %s", exc)
            return None

    async def search(self, customer_id: str, allowed: list[str], q: str,
                     mode: str = "hybrid", page_size: int = 50, page: int = 1,
                     w_vec: float = 0.7, w_text: float = 0.3) -> dict:
        s = settings()
        q = (q or "").strip()
        limit = max(1, min(page_size, 100))
        page = max(1, page)
        skip = (page - 1) * limit
        if not q:
            return {"data": [], "search_method": "none", "note": None,
                    "next_page": None, "scope_accounts": len(allowed)}

        consent_clauses = consent_search_filter(customer_id, allowed)
        prefilter = consent_vector_prefilter(customer_id, allowed)
        text_stage = build_text_stage(q, consent_clauses, s.txn_search_index)
        fetch = skip + limit + 1

        note = None
        query_vector = None
        if mode in ("hybrid", "vector"):
            if await self._has_vectors(customer_id):
                query_vector = self._query_vector(q)
                if query_vector is None:
                    note = "embedding unavailable — showing text-search results"
            else:
                note = "vector / hybrid search is enabled for sample customers only — showing text-search results"

        results: list[dict] = []
        method = "textSearch"

        if query_vector is not None:
            num_candidates = max(fetch * 10, 100)
            vector_stage = build_vector_stage(
                query_vector, prefilter, s.txn_vector_index, fetch, num_candidates)

            if mode == "hybrid":
                try:  # 1) native $rankFusion (MongoDB 8.1+)
                    results = await self._run(
                        build_hybrid_pipeline(text_stage, vector_stage, limit, skip, w_vec, w_text))
                    method = "rankFusion"
                except Exception as exc:
                    logger.warning("$rankFusion failed (%s) — falling back", exc)

            if not results and mode in ("hybrid", "vector"):
                try:  # 2) $vectorSearch only
                    results = await self._run(
                        build_vector_only_pipeline(vector_stage, limit, skip))
                    method = "vectorSearch"
                except Exception as exc:
                    logger.warning("$vectorSearch failed: %s", exc)

        if not results:
            try:  # 3) $search only
                results = await self._run(build_text_only_pipeline(text_stage, limit, skip))
                method = "textSearch"
            except Exception as exc:
                logger.warning("$search failed: %s", exc)

        if not results and query_vector is not None and mode == "hybrid":
            try:  # 4) application-side RRF (k=60) — last resort
                num_candidates = max(fetch * 10, 100)
                vector_stage = build_vector_stage(
                    query_vector, prefilter, s.txn_vector_index, fetch, num_candidates)
                vr = await self._run(build_vector_only_pipeline(vector_stage, fetch, 0))
                tr = await self._run(build_text_only_pipeline(text_stage, fetch, 0))
                merged = rrf_merge(vr, tr, k=60, limit=skip + limit)
                results = merged[skip:skip + limit]
                method = "rrf"
            except Exception as exc:
                logger.warning("app-side RRF failed: %s", exc)

        next_page = page + 1 if len(results) >= limit else None
        return {"data": results, "search_method": method, "note": note,
                "next_page": next_page, "scope_accounts": len(allowed)}
