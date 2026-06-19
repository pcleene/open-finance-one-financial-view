"""Consent backfill + transaction pull (brief §6.2).

Full backfill for a newly authorized consent: pull account objects / balances /
transactions from the OFP (mock), enrich at write time, idempotent upsert
(_id = dp_id::transaction_id), embed the account, insert the balance anchor +
reconstruct the EOD series, refresh the summary, and run the recurring detector.
The per-DP rate limiter (`LIMITER`) lives here; incremental sync imports it.
"""

from datetime import datetime, timezone

from pymongo import UpdateOne

from acme_ofv.config import settings
from acme_ofv.ingestion.amounts import amount_to_decimal, mask_display, parse_dt
from acme_ofv.ingestion.enrichment import categorize
from acme_ofv.ingestion.ofp_client import OFPClient, PerDPRateLimiter, RateLimitCooloff
from acme_ofv.ingestion.profile import (build_consent_boxes, refresh_summary,
                                        upsert_embedded_account)
from acme_ofv.ingestion.recurring import detect_recurring
from acme_ofv.ingestion.snapshots import reconstruct_snapshot_history

LIMITER = PerDPRateLimiter(settings().ofp_rate_limit_per_min)


async def backfill_consent(db, consent: dict) -> dict:
    """Full backfill for a newly authorized consent. Returns stats for the ledger."""
    consent_id = consent["consent_id"]
    dp_id = consent["dp_id"]
    customer_id = consent.get("customer_id")
    if not customer_id:
        profile = await db.customer_profiles.find_one(
            {"customer.hashed_id_number": consent["hashed_id_number"]}, {"_id": 1})
        if not profile:
            return {"error": "no matching customer profile"}
        customer_id = profile["_id"]
        await db.consents.update_one({"_id": consent_id}, {"$set": {"customer_id": customer_id}})

    token_doc = await db.dc_tokens.find_one({"consent_id": consent_id})
    if not token_doc:
        return {"error": "no access token stored for consent"}

    client = OFPClient(token_doc["access_token"], dp_id, LIMITER)
    stats = {"accounts": 0, "transactions": 0, "pages": 0, "balances": 0,
             "snapshots_rebuilt": 0}
    permissions = set(consent["permissions"])
    started = datetime.now(timezone.utc)

    try:
        for acc_ref in consent.get("accounts") or []:
            account_id = acc_ref["account_id"]

            # ---- Account Object ----
            acc_obj = dict(acc_ref)
            if "read_accounts" in permissions:
                r = await client.get(f"/v1/accounts/{account_id}")
                if r.status_code == 200:
                    acc_obj = r.json()
                elif r.status_code == 403:
                    await flag_account_error(db, customer_id, account_id, r)
                    continue

            embedded = {
                "account_id": account_id,
                "account_number": acc_obj.get("account_number", acc_ref.get("account_number")),
                "account_number_masked": mask_display(acc_obj.get("account_number", "")),
                "account_name": acc_obj.get("account_name", acc_ref.get("account_name")),
                "account_holder_name": acc_obj.get("account_holder_name"),
                "institution_name": acc_obj.get("institution_name"),
                "category": acc_obj.get("category"),
                "type": acc_obj.get("type"),
                "subtype": acc_obj.get("subtype"),
                "limit": amount_to_decimal(acc_obj.get("limit")),
                "interest_rate": acc_obj.get("interest_rate"),
                "minimum_payment_amount": amount_to_decimal(acc_obj.get("minimum_payment_amount")),
                "payment_due_date": acc_obj.get("payment_due_date"),
                "loan_details": (
                    {**acc_obj["loan_details"],
                     "loan_amount": amount_to_decimal(acc_obj["loan_details"]["loan_amount"])}
                    if acc_obj.get("loan_details") else None),
                "custom_data": acc_obj.get("custom_data"),
                "dp_id": dp_id,
                "is_internal": False,
                "balances": None,
                "consents": await build_consent_boxes(db, account_id),
                "sync": {"last_full_sync_at": None, "last_txn_date_pulled": None,
                         "last_txn_cursor": None, "consecutive_failures": 0, "last_error": None},
            }

            # ---- Balance Object (point-in-time) + anchor snapshot ----
            anchor_ctx = None
            if "read_balances" in permissions:
                r = await client.get(f"/v1/accounts/{account_id}/balances")
                if r.status_code == 200:
                    bal = r.json()
                    now = datetime.now(timezone.utc)
                    embedded["balances"] = {
                        "current_balance": amount_to_decimal(bal["current_balance"]),
                        "available_balance": amount_to_decimal(bal.get("available_balance")),
                        "statement_balance": amount_to_decimal(bal.get("statement_balance")),
                        "credit_lines_included": bal.get("credit_lines_included", False),
                        "statement_date": bal.get("statement_date"),
                        "custom_data": bal.get("custom_data"),
                        "as_of": now,
                    }
                    stats["balances"] += 1
                    cur = embedded["balances"]["current_balance"]
                    await db.balance_snapshots.insert_one({
                        "as_of": now,
                        "meta": {"customer_id": customer_id, "account_id": account_id,
                                 "dp_id": dp_id, "type": embedded["type"]},
                        "current_balance": cur["amount"],
                        "available_balance": (embedded["balances"]["available_balance"] or cur)["amount"],
                        "credit_debit_indicator": cur["credit_debit_indicator"],
                        "currency": cur["currency"],
                    })
                    # reconstruction (from the rows pulled below) replaces the old
                    # synthesized random walk — see reconstruct_snapshot_history
                    anchor_ctx = {"amount": cur["amount"].to_decimal(),
                                  "indicator": cur["credit_debit_indicator"]}

            # ---- Transactions (cursor pagination, newest-first) ----
            if "read_transactions" in permissions:
                n, pages, last_date = await pull_transactions(
                    db, client, consent, customer_id, embedded)
                stats["transactions"] += n
                stats["pages"] += pages
                embedded["sync"]["last_txn_date_pulled"] = last_date

            # ---- Reconstruct the EOD balance series from the pulled rows ----
            # (brief §3) Replaces synthesize_snapshot_history's random walk with a
            # backward walk from the real anchor. Anchor-only when read_transactions
            # is absent or there were no rows to walk; synthesize_snapshot_history
            # stays in snapshots.py as the documented fallback.
            if anchor_ctx is not None:
                stats["snapshots_rebuilt"] += await reconstruct_snapshot_history(
                    db, customer_id, embedded, anchor_ctx["amount"],
                    anchor_ctx["indicator"], dp_id)

            embedded["sync"]["last_full_sync_at"] = datetime.now(timezone.utc)
            await upsert_embedded_account(db, customer_id, embedded)
            stats["accounts"] += 1

        await refresh_summary(db, customer_id)
        await detect_recurring(db, customer_id)
        # uw_features is built on demand at loan inquiry (brief §10), not on ingest.
    except RateLimitCooloff as exc:
        stats["error"] = str(exc)
    finally:
        await db.ofp_pull_ledger.insert_one({
            "at": started, "kind": "backfill", "consent_id": consent_id,
            "dp_id": dp_id, "customer_id": customer_id,
            "calls": client.calls, "retries_429": client.retries_429,
            "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            **stats,
        })
        await client.aclose()
    return stats


async def flag_account_error(db, customer_id: str, account_id: str, resp) -> None:
    code = (resp.json().get("errors") or [{}])[0].get("code", f"http_{resp.status_code}")
    await db.customer_profiles.update_one(
        {"_id": customer_id, "accounts.account_id": account_id},
        {"$set": {"accounts.$.sync.last_error": code},
         "$inc": {"accounts.$.sync.consecutive_failures": 1}},
    )


async def pull_transactions(db, client: OFPClient, consent: dict, customer_id: str,
                            embedded: dict, from_date: str | None = None):
    """Paginate /transactions, enrich, idempotent bulk upserts in pages."""
    s = settings()
    account_id = embedded["account_id"]
    hashed = consent.get("hashed_id_number")
    params: dict = {"page_size": s.ofp_page_size}
    if from_date:
        params["from_date"] = from_date
    # Slimmed consent stamp (brief §1): a single consent_id string on external
    # rows only — pure provenance ("under which lawful basis was this row
    # collected"). The gate never reads it (live state lives in the profile
    # boxes); consent_purpose / permissions / expiration are all recoverable by
    # joining consent_id -> consents, so storing them per row is redundant.
    consent_id = consent["consent_id"]
    account_ctx = {
        "account_id": account_id, "dp_id": embedded["dp_id"],
        "institution_name": embedded["institution_name"], "type": embedded["type"],
        "subtype": embedded["subtype"],
        "account_number_masked": embedded["account_number_masked"],
        "is_internal": False,
    }

    total, pages, newest = 0, 0, None
    cursor: str | None = embedded["sync"].get("last_txn_cursor")
    while True:
        p = dict(params)
        if cursor:
            p["next_page_params"] = cursor  # opaque — replayed verbatim
        r = await client.get(f"/v1/accounts/{account_id}/transactions", params=p)
        if r.status_code != 200:
            await flag_account_error(db, customer_id, account_id, r)
            break
        body = r.json()
        rows = body.get("data", [])
        pages += 1
        if rows:
            ops = []
            for t in rows:
                tdate = parse_dt(t["transaction_date"])
                newest = max(newest, tdate) if newest else tdate
                doc = {
                    "_id": f"{embedded['dp_id']}::{t['transaction_id']}",
                    "schema_version": 2,
                    "customer_id": customer_id,
                    "hashed_id_number": hashed,
                    **t,
                    "transaction_date": tdate,
                    "amount": amount_to_decimal(t["amount"]),
                    "foreign_currency_amount": amount_to_decimal(t.get("foreign_currency_amount")),
                    "account": account_ctx,
                    "consent_id": consent_id,
                    "enrichment": categorize(t),
                    "ingest": {
                        "ingested_at": datetime.now(timezone.utc),
                        "x_fapi_interaction_id": getattr(r, "interaction_id", None),
                        "source": "ofp_pull",
                    },
                }
                if "OWN ACCOUNT" in (t.get("description") or ""):
                    doc["enrichment"]["is_transfer_own_account"] = True
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
            await db.transactions.bulk_write(ops, ordered=False)
            total += len(rows)
        cursor = (body.get("meta") or {}).get("next_page_params")
        await db.customer_profiles.update_one(
            {"_id": customer_id, "accounts.account_id": account_id},
            {"$set": {"accounts.$.sync.last_txn_cursor": cursor}})
        if not cursor:
            break
    return total, pages, newest
