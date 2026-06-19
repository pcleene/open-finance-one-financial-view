"""Pure aggregation-pipeline builders for transaction search (brief §5).

No I/O here — the service assembles + runs these and owns the fallback chain.
Modeled on reference-demo's pipeline_builders; the consent scope (customer_id +
allowed account_ids) is the shared filter, applied as a `$search` compound
`filter` in the text leg and a `$vectorSearch.filter` pre-filter in the vector
leg (same scope, two syntaxes — the NPF pattern).
"""

from __future__ import annotations

# raw text fields indexed by txn_text (no concatenated search_text field)
TEXT_PATHS = [
    "description",
    "enrichment.merchant_normalized",
    "enrichment.subcategory",
    "enrichment.category",
    "recipient_reference",
]

# drop the large vector from results; keep everything else (+ the fused score)
RESULT_PROJECTION = {"embedding": 0}


def consent_search_filter(customer_id: str, allowed: list[str]) -> list[dict]:
    """`$search` compound filter clauses for the text leg (token fields)."""
    return [
        {"in": {"path": "customer_id", "value": [customer_id]}},
        {"in": {"path": "account.account_id", "value": list(allowed)}},
    ]


def consent_vector_prefilter(customer_id: str, allowed: list[str]) -> dict:
    """`$vectorSearch.filter` pre-filter for the vector leg (MongoDB query ops)."""
    return {"customer_id": customer_id, "account.account_id": {"$in": list(allowed)}}


def build_text_stage(query: str, consent_clauses: list[dict], index: str) -> dict:
    return {"$search": {
        "index": index,
        "compound": {
            "must": [{"text": {"query": query, "path": TEXT_PATHS,
                               "fuzzy": {"maxEdits": 1}}}],
            "filter": consent_clauses,
        },
    }}


def build_vector_stage(query_vector: list[float], prefilter: dict, index: str,
                       limit: int, num_candidates: int) -> dict:
    stage = {"$vectorSearch": {
        "index": index,
        "path": "embedding",
        "queryVector": query_vector,
        "numCandidates": num_candidates,
        "limit": limit,
    }}
    if prefilter:
        stage["$vectorSearch"]["filter"] = prefilter
    return stage


def build_hybrid_pipeline(text_stage: dict, vector_stage: dict, limit: int,
                          skip: int = 0, w_vec: float = 0.7, w_text: float = 0.3) -> list[dict]:
    """$rankFusion: text + vector hybrid, weighted. $skip/$limit AFTER fusion;
    fused rank-fusion score read via {$meta: "score"}."""
    fetch = skip + limit + 1
    pipeline: list[dict] = [
        {"$rankFusion": {
            "input": {"pipelines": {
                "textSearch": [text_stage, {"$limit": fetch}],
                "vectorSearch": [vector_stage],
            }},
            "combination": {"weights": {"vectorSearch": w_vec, "textSearch": w_text}},
        }},
        {"$addFields": {"search_score": {"$meta": "score"}}},
    ]
    if skip:
        pipeline.append({"$skip": skip})
    pipeline.append({"$limit": limit})
    pipeline.append({"$project": RESULT_PROJECTION})
    return pipeline


def build_vector_only_pipeline(vector_stage: dict, limit: int, skip: int = 0) -> list[dict]:
    pipeline: list[dict] = [
        vector_stage,
        {"$addFields": {"search_score": {"$meta": "vectorSearchScore"}}},
    ]
    if skip:
        pipeline.append({"$skip": skip})
    pipeline.append({"$limit": limit})
    pipeline.append({"$project": RESULT_PROJECTION})
    return pipeline


def build_text_only_pipeline(text_stage: dict, limit: int, skip: int = 0) -> list[dict]:
    pipeline: list[dict] = [
        text_stage,
        {"$addFields": {"search_score": {"$meta": "searchScore"}}},
    ]
    if skip:
        pipeline.append({"$skip": skip})
    pipeline.append({"$limit": limit})
    pipeline.append({"$project": RESULT_PROJECTION})
    return pipeline


def rrf_merge(vector_results: list[dict], text_results: list[dict],
              k: int = 60, limit: int = 50) -> list[dict]:
    """Application-side Reciprocal Rank Fusion (last-resort fallback for clusters
    without $rankFusion) — merge two ranked lists by sum of 1/(k+rank)."""
    scores: dict = {}
    doc_map: dict = {}
    for ranked in (vector_results, text_results):
        for rank, doc in enumerate(ranked):
            _id = doc["_id"]
            doc_map[_id] = doc
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank + 1)
    out = []
    for _id, sc in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        doc = doc_map[_id]
        doc["search_score"] = round(sc, 6)
        out.append(doc)
    return out
