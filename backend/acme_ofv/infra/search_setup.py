"""Create the Atlas Search (txn_text) + Vector Search (txn_vector) indexes on
acme_ofv.transactions (brief §5). Mirrors reference-demo/backend/seed/create_indexes.py.

Atlas Search + $vectorSearch are Lucene/HNSW — never $regex (brief §15). The
consent hard-filter fields (customer_id, account.account_id) are declared as
`token` (text index) and `filter` (vector index) so the gate's allowed-account
set pre-filters BOTH legs of the hybrid query.

Run:  uv run python -m acme_ofv.infra.search_setup
"""

import asyncio

from acme_ofv.config import settings
from acme_ofv.db import make_async_client, ofv_db


def text_index_def() -> dict:
    return {
        "mappings": {
            "dynamic": False,
            "fields": {
                "customer_id": [{"type": "token"}],
                "account": {"type": "document", "fields": {
                    "account_id": [{"type": "token"}],
                    "dp_id": [{"type": "token"}],
                }},
                "description": [{"type": "string", "analyzer": "lucene.standard"}],
                "recipient_reference": [{"type": "string", "analyzer": "lucene.standard"}],
                "enrichment": {"type": "document", "fields": {
                    "merchant_normalized": [{"type": "string", "analyzer": "lucene.standard"}],
                    "subcategory": [{"type": "string", "analyzer": "lucene.standard"}],
                    "category": [
                        {"type": "string", "analyzer": "lucene.standard"},
                        {"type": "stringFacet"},
                        {"type": "token"},
                    ],
                }},
            },
        }
    }


def vector_index_def() -> dict:
    return {
        "fields": [
            {"type": "vector", "path": "embedding",
             "numDimensions": settings().voyage_dimensions, "similarity": "cosine"},
            {"type": "filter", "path": "customer_id"},
            {"type": "filter", "path": "account.account_id"},
        ]
    }


async def main() -> None:
    s = settings()
    client = make_async_client()
    db = ofv_db(client)
    coll = db["transactions"]
    plan = [
        (s.txn_search_index, "search", text_index_def()),
        (s.txn_vector_index, "vectorSearch", vector_index_def()),
    ]
    for name, idx_type, definition in plan:
        try:
            try:
                await coll.drop_search_index(name)
                print(f"  dropped existing index {name}")
                await asyncio.sleep(2)
            except Exception:
                pass
            await coll.create_search_index(
                {"definition": definition, "name": name, "type": idx_type})
            print(f"  created {idx_type} index {name} on transactions")
        except Exception as exc:
            print(f"  WARN could not create {name}: {exc}")
            print(f"  -> create manually in Atlas UI (collection=transactions, "
                  f"name={name}, type={idx_type})")
    print("search index creation initiated (Atlas builds them asynchronously)")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
