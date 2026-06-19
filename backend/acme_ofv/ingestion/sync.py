"""Scheduled incremental sync (brief §6.2): top-up pulls from last-pulled minus
an overlap window, idempotent. Reuses the per-DP limiter + pull from backfill."""

from datetime import datetime, timedelta, timezone

from acme_ofv.config import settings
from acme_ofv.ingestion.backfill import LIMITER, pull_transactions
from acme_ofv.ingestion.ofp_client import OFPClient
from acme_ofv.ingestion.recurring import detect_recurring


async def incremental_sync_consent(db, consent: dict) -> dict:
    """Scheduled incremental: from_date = last pulled − overlap window (idempotent upserts)."""
    profile = await db.customer_profiles.find_one(
        {"_id": consent.get("customer_id")}, {"accounts": 1})
    if not profile:
        return {"error": "profile missing"}
    token_doc = await db.dc_tokens.find_one({"consent_id": consent["consent_id"]})
    if not token_doc:
        return {"error": "no token"}
    client = OFPClient(token_doc["access_token"], consent["dp_id"], LIMITER)
    total = 0
    started = datetime.now(timezone.utc)
    try:
        in_scope = {a["account_id"] for a in consent.get("accounts") or []}
        for acc in profile.get("accounts", []):
            if acc["account_id"] not in in_scope:
                continue
            last = (acc.get("sync") or {}).get("last_txn_date_pulled")
            from_date = ((last or datetime.now(timezone.utc) - timedelta(days=180))
                         - timedelta(days=settings().incremental_overlap_days)).date().isoformat()
            n, _, newest = await pull_transactions(db, client, consent, profile["_id"], acc,
                                                   from_date=from_date)
            total += n
            if newest:
                await db.customer_profiles.update_one(
                    {"_id": profile["_id"], "accounts.account_id": acc["account_id"]},
                    {"$set": {"accounts.$.sync.last_txn_date_pulled": newest,
                              "accounts.$.sync.last_full_sync_at": datetime.now(timezone.utc)}})
    finally:
        # pull ledger parity with backfill — gives the ingestion simulator its
        # per-DP call / 429 / latency stats
        await db.ofp_pull_ledger.insert_one({
            "at": started, "kind": "incremental", "consent_id": consent["consent_id"],
            "dp_id": consent["dp_id"], "customer_id": consent.get("customer_id"),
            "calls": client.calls, "retries_429": client.retries_429,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "transactions": total,
        })
        await client.aclose()
    if total:
        await detect_recurring(db, consent["customer_id"])
        # uw_features build moved to loan-inquiry time (brief §10).
    return {"transactions": total, "calls": client.calls, "retries_429": client.retries_429}
