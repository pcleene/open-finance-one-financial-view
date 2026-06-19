"""Drive the full PAR → authorize → token consent flow for every row in
seed_link_plan (the cohort's planned consents), via the REAL endpoints:

  DC API /consents/{cid}/link  →  mock PAR  →  decision POST (auto-approve all
  accounts, like a user ticking every box)  →  303 → DC callback → token
  exchange → post-image published → worker backfills.

Requires mock_ofp (:8100), api (:8000) and the worker running.

Run:  uv run python -m acme_ofv.seed.link_all [--limit N]
"""

import argparse
import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

from acme_ofv.config import settings
from acme_ofv.db import make_async_client, mock_db, ofv_db

S = settings()
SEM = asyncio.Semaphore(8)


async def link_one(odb, mdb, plan: dict) -> str:
    async with SEM:
        async with httpx.AsyncClient(timeout=60) as hc:
            r = await hc.post(
                f"{S.dc_base_url}/consents/{plan['customer_id']}/link",
                json={"dp_id": plan["dp_id"],
                      "consent_purpose": plan["consent_purpose"],
                      "permissions": plan["permissions"],
                      "validity_days": plan.get("validity_days", 180)})
            if r.status_code != 200:
                return f"link failed: {r.text[:120]}"
            authorize_url = r.json()["authorize_url"]
            request_uri = parse_qs(urlparse(authorize_url).query)["request_uri"][0]

            # the user's accounts at this DP (what the picker page would show)
            profile = await odb.customer_profiles.find_one(
                {"_id": plan["customer_id"]}, {"customer.hashed_id_number": 1})
            accounts = await mdb.accounts.find(
                {"dp_id": plan["dp_id"],
                 "hashed_id_number": profile["customer"]["hashed_id_number"]},
                {"account_id": 1}).to_list(None)
            if not accounts:
                return "no accounts at DP"

            form = {"request_uri": request_uri, "decision": "approve",
                    "account_ids": [a["account_id"] for a in accounts]}
            r2 = await hc.post(f"{S.ofp_base_url}/v1/oauth/authorize/decision",
                               data=form, follow_redirects=False)
            if r2.status_code != 303:
                return f"decision failed: {r2.status_code} {r2.text[:120]}"
            r3 = await hc.get(r2.headers["location"])  # → DC callback (token exchange)
            if r3.status_code != 200 or "linked" not in r3.text.lower():
                return f"callback failed: {r3.status_code} {r3.text[:120]}"

        await odb.seed_link_plan.update_one({"_id": plan["_id"]},
                                            {"$set": {"status": "done"}})
        return "ok"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    client = make_async_client()
    odb, mdb = ofv_db(client), mock_db(client)

    q = {"status": "pending"}
    plans = await odb.seed_link_plan.find(q).to_list(args.limit or None)
    print(f"linking {len(plans)} consents …")
    results = await asyncio.gather(*(link_one(odb, mdb, p) for p in plans))
    ok = sum(1 for r in results if r == "ok")
    print(f"done: {ok}/{len(plans)} linked")
    for r in sorted({r for r in results if r != 'ok'}):
        print(f"  issue: {r}")

    # wait for the worker's backfills to drain
    for _ in range(120):
        backfilled = await odb.ofp_pull_ledger.count_documents({"kind": "backfill"})
        authorized = await odb.consents.count_documents({"status": "authorized"})
        print(f"  backfills {backfilled}/{authorized}", end="\r")
        if backfilled >= authorized > 0:
            break
        await asyncio.sleep(5)
    print()
    txns = await odb.transactions.estimated_document_count()
    profiles_ext = await odb.customer_profiles.count_documents(
        {"summary.external_account_count": {"$gt": 0}})
    print(f"transactions in acme_ofv: {txns:,} · profiles with external accounts: {profiles_ext}")

    # provenance breakdown — makes the Acme-internal vs external-DP split explicit
    # (external rows only land here via this ingestion pass, never via run_seed).
    breakdown = await odb.transactions.aggregate([
        {"$group": {"_id": {"source": "$ingest.source", "dp_id": "$account.dp_id"},
                    "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]).to_list(None)
    print("transactions by source · dp_id:")
    for row in breakdown:
        src = row["_id"].get("source") or "?"
        dp = row["_id"].get("dp_id") or "?"
        print(f"  {src:12} {dp:22} {row['n']:>10,}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
