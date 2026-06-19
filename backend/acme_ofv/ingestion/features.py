"""uw_features build (brief §10.1) — per-account decomposable underwriting
components, rolled up over the 6-month window. Built on demand at loan inquiry
(the change-stream updater keeps monthly buckets warm between rebuilds)."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bson import Decimal128

from acme_ofv.db import aggregate_list
from acme_ofv.ingestion.amounts import d128


async def rebuild_uw_features(db, customer_id: str) -> None:
    profile = await db.customer_profiles.find_one({"_id": customer_id})
    if not profile:
        return
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=185)

    monthly = await aggregate_list(db.transactions, [
        {"$match": {"customer_id": customer_id,
                    "transaction_date": {"$gte": window_start},
                    "enrichment.is_transfer_own_account": {"$ne": True}}},
        {"$group": {
            "_id": {"account_id": "$account.account_id", "month": "$enrichment.month"},
            "inflow": {"$sum": {"$cond": [
                {"$eq": ["$credit_debit_indicator", "credit"]}, "$amount.amount", 0]}},
            "outflow": {"$sum": {"$cond": [
                {"$eq": ["$credit_debit_indicator", "debit"]}, "$amount.amount", 0]}},
            "txn_count": {"$sum": 1},
            "salary_credit": {"$sum": {"$cond": [
                {"$eq": ["$enrichment.category", "salary_income"]}, "$amount.amount", 0]}},
            "gambling_spend": {"$sum": {"$cond": [
                {"$eq": ["$enrichment.category", "gambling"]}, "$amount.amount", 0]}},
            "cash_withdrawal": {"$sum": {"$cond": [
                {"$eq": ["$enrichment.category", "cash"]}, "$amount.amount", 0]}},
            "fx_spend": {"$sum": {"$cond": [
                {"$ne": ["$foreign_currency_amount", None]}, "$amount.amount", 0]}},
            "salary_credit_date": {"$max": {"$cond": [
                {"$eq": ["$enrichment.category", "salary_income"]},
                {"$dayOfMonth": "$transaction_date"}, None]}},
        }},
    ])

    by_account: dict[str, list[dict]] = defaultdict(list)
    for m in monthly:
        by_account[m["_id"]["account_id"]].append({
            "month": m["_id"]["month"],
            "inflow": m["inflow"], "outflow": m["outflow"], "txn_count": m["txn_count"],
            "salary_credit": m["salary_credit"], "salary_credit_date": m["salary_credit_date"],
            "gambling_spend": m["gambling_spend"], "cash_withdrawal": m["cash_withdrawal"],
            "fx_spend": m["fx_spend"],
        })

    accounts = []
    for acc in profile.get("accounts", []):
        comp: dict = {
            "account_id": acc["account_id"], "dp_id": acc["dp_id"],
            "type": acc["type"], "subtype": acc["subtype"],
            "is_internal": acc.get("is_internal", False),
            "monthly": sorted(by_account.get(acc["account_id"], []),
                              key=lambda x: x["month"], reverse=True)[:6],
        }
        bal = acc.get("balances") or {}
        if acc["type"] == "credit" and acc.get("limit"):
            limit = acc["limit"]["amount"].to_decimal()
            owed = (bal.get("current_balance") or {}).get("amount")
            owed = owed.to_decimal() if owed else Decimal("0")
            comp["credit"] = {
                "limit": Decimal128(limit), "current_owed": Decimal128(owed),
                "minimum_payment_amount": (acc.get("minimum_payment_amount") or {}).get("amount"),
                "utilization_now": round(float(owed / limit), 4) if limit else None,
            }
        if acc["type"] == "loan":
            comp["loan"] = {
                "installment": (acc.get("minimum_payment_amount") or {}).get("amount"),
                "payment_due_date": acc.get("payment_due_date"),
                "loan_amount": ((acc.get("loan_details") or {}).get("loan_amount") or {}).get("amount"),
            }
        if acc["type"] == "deposit":
            stats = await aggregate_list(db.balance_snapshots, [
                {"$match": {"meta.account_id": acc["account_id"],
                            "as_of": {"$gte": now - timedelta(days=90)}}},
                {"$group": {"_id": None,
                            "avg_eod": {"$avg": {"$toDouble": "$current_balance"}},
                            "min_eod": {"$min": {"$toDouble": "$current_balance"}},
                            "stddev_eod": {"$stdDevPop": {"$toDouble": "$current_balance"}},
                            "days_below_500": {"$sum": {"$cond": [
                                {"$lt": [{"$toDouble": "$current_balance"}, 500]}, 1, 0]}}}},
            ])
            if stats:
                st = stats[0]
                comp["balance_stats_90d"] = {
                    "avg_eod": d128(round(st["avg_eod"], 2)),
                    "min_eod": d128(round(st["min_eod"], 2)),
                    "stddev_eod": round(st["stddev_eod"] or 0.0, 2),
                    "days_below_500": st["days_below_500"],
                }
        accounts.append(comp)

    await db.uw_features.replace_one(
        {"_id": customer_id},
        {"_id": customer_id, "as_of": now, "feature_version": 3, "window_months": 6,
         "accounts": accounts},
        upsert=True,
    )
