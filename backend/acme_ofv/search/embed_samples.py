"""One-off: embed transactions for the UI-switcher sample customers only,
capped at `vector_max_total` rows total (brief §5) — keeps the Voyage bill
bounded. Off the hot ingest path.

Allowlist = the same `/customers` query the header dropdown uses (first N
profiles by _id). Per-customer budget ~= vector_max_total // N, most-recent
first, with a hard global stop at vector_max_total. Each row's embedding input
is composed inline from its own fields (no stored concatenated field).

Run:  uv run python -m acme_ofv.search.embed_samples
"""

import asyncio

from pymongo import UpdateOne

from acme_ofv.config import settings
from acme_ofv.db import make_async_client, ofv_db
from acme_ofv.search.embedding_service import embed_batch
from acme_ofv.search.text import compose_embedding_text


async def sample_customer_ids(db, count: int) -> list[str]:
    """Same set the /customers endpoint serves (header dropdown) — first N by _id."""
    profiles = await db.customer_profiles.find({}, {"_id": 1}).sort("_id", 1).limit(count).to_list(None)
    return [p["_id"] for p in profiles]


async def select_rows(db, customer_ids: list[str], max_total: int) -> list[dict]:
    """≤ max_total most-recent rows across the allowlist (per-customer budget)."""
    if not customer_ids:
        return []
    per_customer = max(1, max_total // len(customer_ids))
    selected: list[dict] = []
    for cid in customer_ids:
        if len(selected) >= max_total:
            break
        budget = min(per_customer, max_total - len(selected))
        docs = await db.transactions.find(
            {"customer_id": cid}, {"embedding": 0},
        ).sort([("transaction_date", -1), ("_id", -1)]).limit(budget).to_list(None)
        selected.extend(docs)
    return selected[:max_total]


async def main() -> None:
    s = settings()
    if not s.voyage_api_key:
        print("VOYAGE_API_KEY not set — aborting (see .env / brief §5)")
        return

    client = make_async_client()
    db = ofv_db(client)
    try:
        customer_ids = await sample_customer_ids(db, s.vector_sample_customer_count)
        rows = await select_rows(db, customer_ids, s.vector_max_total)
        print(f"embedding {len(rows):,} transactions across {len(customer_ids)} customers "
              f"(cap {s.vector_max_total:,}) with {s.voyage_index_model} …")
        if not rows:
            print("no rows to embed — run the seed + link pipeline first")
            return

        texts = [compose_embedding_text(d) for d in rows]
        embeddings = embed_batch(texts, batch_size=128)

        ops = [UpdateOne({"_id": d["_id"]}, {"$set": {"embedding": emb}})
               for d, emb in zip(rows, embeddings)]
        for i in range(0, len(ops), 500):
            await db.transactions.bulk_write(ops[i:i + 500], ordered=False)
        print(f"done — {len(ops):,} transactions embedded "
              f"({s.voyage_dimensions}-d, vector index '{s.txn_vector_index}')")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
