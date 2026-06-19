"""PFM service (brief §9) — every method passes the consent gate (Path B) and
every computation is an aggregation over the consolidated collections. No regex
anywhere: equality/range on indexed fields only.

Relocated verbatim from the former api/pfm.py handlers (behavior-preserving).
"""

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bson import Decimal128
from fastapi import HTTPException

from acme_ofv.api.serialize import jsonable
from acme_ofv.consent.gate import require_scope, scope_from_profile
from acme_ofv.db import aggregate_list
from acme_ofv.query_log import log_query


def month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def compute_budgets(db, customer_id: str, allowed: list[str],
                          budget_defs: list, month: str | None = None) -> dict:
    """Budget vs month-to-date spend for an ALREADY-resolved pfm scope.

    Takes the scope + budget definitions (no profile read of its own), so it can be
    driven either by the standalone /budgets read OR reused from the One View
    aggregate result — letting the home page serve budgets without a second
    customer_profiles read. The only DB hit here is one transactions aggregate."""
    month = month or month_now()
    if not allowed or not budget_defs:
        return {"month": month, "budgets": []}
    spent = await aggregate_list(db.transactions, [
        {"$match": {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                    "enrichment.month": month, "credit_debit_indicator": "debit",
                    "enrichment.is_transfer_own_account": False}},
        {"$group": {"_id": "$enrichment.category", "total": {"$sum": "$amount.amount"}}},
    ])
    spent_map = {r["_id"]: float(r["total"].to_decimal()) for r in spent}
    out = []
    for b in budget_defs:
        limit = float(b["monthly_limit"].to_decimal())
        used = spent_map.get(b["category"], 0.0)
        out.append({"category": b["category"], "monthly_limit": f"{limit:.2f}",
                    "spent": f"{used:.2f}", "pct": round(used / limit, 3) if limit else None,
                    "alert": limit > 0 and used / limit >= b.get("alert_threshold", 0.8)})
    return {"month": month, "budgets": out}


def recurring_groups_pipeline(customer_id: str, allowed: list[str]) -> list[dict]:
    """Shared recurring-merchant rollup (brief §9). Both the Recurring card and
    Safe-to-Spend run this, so they issue the IDENTICAL pipeline against the same
    scope — the Query Inspector then collapses them into ONE card instead of two
    near-duplicate aggregates, and it's a single query to explain in a demo.
    `allowed` must be passed as the resolve_consent_scope LIST (same order) so the
    `$in` arrays match exactly across the two calls."""
    return [
        {"$match": {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                    "enrichment.is_recurring": True}},
        {"$sort": {"transaction_date": -1}},
        {"$group": {
            "_id": "$enrichment.recurring_group_id",
            "merchant": {"$first": "$enrichment.merchant_normalized"},
            "description": {"$first": "$description"},
            "category": {"$first": "$enrichment.category"},
            "period": {"$first": "$enrichment.recurring_period"},
            "amount": {"$first": "$amount.amount"},
            "last_date": {"$max": "$transaction_date"},
            "count": {"$sum": 1},
            "institution": {"$first": "$account.institution_name"},
        }},
        {"$sort": {"amount": -1}},
    ]


class PfmService:
    def __init__(self, db):
        self.db = db

    async def require_pfm_scope(self, customer_id: str) -> list[str]:
        return await require_scope(self.db, customer_id, "pfm")

    async def spend_by_category(self, customer_id: str, month: str | None = None) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        month = month or month_now()
        t0 = time.perf_counter()
        rows = await aggregate_list(db.transactions, [
            {"$match": {
                "customer_id": customer_id,
                "account.account_id": {"$in": allowed},
                "enrichment.month": month,
                "credit_debit_indicator": "debit",
                "is_settled": True,
                "enrichment.is_transfer_own_account": False,
            }},
            {"$group": {
                "_id": "$enrichment.category",
                "total": {"$sum": "$amount.amount"},
                "count": {"$sum": 1},
                "top_merchants": {"$topN": {"n": 3, "sortBy": {"amount.amount": -1},
                                  "output": {"m": "$enrichment.merchant_normalized",
                                             "a": "$amount.amount"}}},
            }},
            {"$sort": {"total": -1}},
        ])
        prev_month = (datetime.strptime(month + "-01", "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
        prev = await aggregate_list(db.transactions, [
            {"$match": {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                        "enrichment.month": prev_month, "credit_debit_indicator": "debit",
                        "is_settled": True, "enrichment.is_transfer_own_account": False}},
            {"$group": {"_id": "$enrichment.category", "total": {"$sum": "$amount.amount"}}},
        ])
        prev_map = {p["_id"]: float(p["total"].to_decimal()) for p in prev}
        out = []
        for r in rows:
            cur = float(r["total"].to_decimal())
            pm = prev_map.get(r["_id"])
            out.append({"category": r["_id"], "total": f"{cur:.2f}", "count": r["count"],
                        "top_merchants": jsonable(r["top_merchants"]),
                        "mom_delta_pct": round((cur - pm) / pm * 100, 1) if pm else None})
        return {"month": month, "categories": out, "scope_accounts": len(allowed),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    async def cashflow(self, customer_id: str, months: int = 12,
                       institution: str | None = None, indicator: str | None = None) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        start = (datetime.now(timezone.utc) - timedelta(days=31 * months)).strftime("%Y-%m")
        match: dict = {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                       "enrichment.month": {"$gte": start},
                       "enrichment.is_transfer_own_account": False}
        if institution:
            match["account.dp_id"] = institution
        if indicator in ("credit", "debit"):
            match["credit_debit_indicator"] = indicator
        rows = await aggregate_list(db.transactions, [
            {"$match": match},
            {"$group": {
                "_id": {"month": "$enrichment.month", "ind": "$credit_debit_indicator",
                        "institution": "$account.institution_name"},
                "total": {"$sum": "$amount.amount"},
            }},
            {"$group": {
                "_id": "$_id.month",
                "flows": {"$push": {"ind": "$_id.ind", "institution": "$_id.institution",
                                    "total": "$total"}},
            }},
            {"$sort": {"_id": 1}},
        ])
        series = []
        for m in rows:
            inflow = sum(Decimal(str(f["total"].to_decimal())) for f in m["flows"] if f["ind"] == "credit")
            outflow = sum(Decimal(str(f["total"].to_decimal())) for f in m["flows"] if f["ind"] == "debit")
            by_inst: dict[str, dict] = {}
            for f in m["flows"]:
                e = by_inst.setdefault(f["institution"], {"in": Decimal(0), "out": Decimal(0)})
                e["in" if f["ind"] == "credit" else "out"] += f["total"].to_decimal()
            series.append({
                "month": m["_id"], "money_in": f"{inflow:.2f}", "money_out": f"{outflow:.2f}",
                "net": f"{inflow - outflow:.2f}",
                "by_institution": {k: {"in": f'{v["in"]:.2f}', "out": f'{v["out"]:.2f}'}
                                   for k, v in by_inst.items()},
            })
        return {"series": series}

    async def net_worth(self, customer_id: str, weeks: int = 13) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "one_view")
        profile = await db.customer_profiles.find_one({"_id": customer_id}, {"summary": 1})
        start = datetime.now(timezone.utc) - timedelta(weeks=weeks)
        trend = await aggregate_list(db.balance_snapshots, [
            {"$match": {"meta.customer_id": customer_id,
                        "meta.account_id": {"$in": allowed},
                        "as_of": {"$gte": start}}},
            {"$group": {
                "_id": {"week": {"$dateTrunc": {"date": "$as_of", "unit": "week"}},
                        "account_id": "$meta.account_id"},
                "type": {"$first": "$meta.type"},
                "indicator": {"$last": "$credit_debit_indicator"},
                "balance": {"$avg": {"$toDouble": "$current_balance"}},
            }},
            {"$group": {
                "_id": "$_id.week",
                "assets": {"$sum": {"$cond": [{"$eq": ["$indicator", "credit"]}, "$balance", 0]}},
                "liabilities": {"$sum": {"$cond": [{"$eq": ["$indicator", "debit"]}, "$balance", 0]}},
            }},
            {"$project": {"assets": {"$round": ["$assets", 2]},
                          "liabilities": {"$round": ["$liabilities", 2]},
                          "net": {"$round": [{"$subtract": ["$assets", "$liabilities"]}, 2]}}},
            {"$sort": {"_id": 1}},
        ])
        return {"now": jsonable((profile or {}).get("summary", {}).get("net_position")),
                "trend": jsonable(trend)}

    async def recurring(self, customer_id: str) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        groups = await aggregate_list(db.transactions,
                                      recurring_groups_pipeline(customer_id, allowed))
        out = []
        seen_cat: dict[str, list] = {}
        for g in groups:
            nxt = g["last_date"] + timedelta(days=30 if g["period"] == "monthly" else 7)
            days_inactive = (datetime.now(timezone.utc) - g["last_date"].replace(tzinfo=timezone.utc)).days
            item = {**jsonable(g), "next_expected": nxt.date().isoformat(),
                    "zombie": days_inactive > 60,
                    "label": g["merchant"] or g["description"]}
            seen_cat.setdefault(f'{g["category"]}:{g["period"]}', []).append(item)
            out.append(item)
        for items in seen_cat.values():
            if len([i for i in items if not i["zombie"]]) > 1 and items[0]["category"] == "entertainment":
                for i in items:
                    i["duplicate_candidate"] = True
        return {"groups": out}

    async def top_merchants(self, customer_id: str, month: str | None = None) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        month = month or month_now()
        rows = await aggregate_list(db.transactions, [
            {"$match": {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                        "enrichment.month": month, "credit_debit_indicator": "debit",
                        "enrichment.merchant_normalized": {"$ne": None}}},
            {"$group": {"_id": "$enrichment.merchant_normalized",
                        "total": {"$sum": "$amount.amount"}, "count": {"$sum": 1},
                        "category": {"$first": "$enrichment.category"}}},
            {"$sort": {"total": -1}}, {"$limit": 15},
        ])
        return {"month": month, "merchants": jsonable(rows)}

    async def transactions(self, customer_id: str, category: str | None = None,
                           institution: str | None = None, account_id: str | None = None,
                           indicator: str | None = None, min_amount: float | None = None,
                           max_amount: float | None = None, from_date: str | None = None,
                           to_date: str | None = None, cursor: str | None = None,
                           page_size: int = 50) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        q: dict = {"customer_id": customer_id, "account.account_id": {"$in": allowed}}
        if account_id:
            if account_id not in allowed:
                return {"data": [], "next_cursor": None}
            q["account.account_id"] = account_id
        if category:
            q["enrichment.category"] = category
        if institution:
            q["account.dp_id"] = institution
        if indicator in ("credit", "debit"):
            q["credit_debit_indicator"] = indicator
        if min_amount is not None or max_amount is not None:
            amt = {}
            if min_amount is not None:
                amt["$gte"] = Decimal128(str(round(min_amount, 2)))
            if max_amount is not None:
                amt["$lte"] = Decimal128(str(round(max_amount, 2)))
            q["amount.amount"] = amt
        if from_date:
            q.setdefault("transaction_date", {})["$gte"] = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        if to_date:
            q.setdefault("transaction_date", {})["$lte"] = datetime.fromisoformat(to_date).replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        if cursor:
            cdate, cid = cursor.split("|", 1)
            q["$or"] = [
                {"transaction_date": {"$lt": datetime.fromisoformat(cdate)}},
                {"transaction_date": datetime.fromisoformat(cdate), "_id": {"$lt": cid}},
            ]
        # Project only what the list renders (+ _id/date for the keyset cursor).
        # Full docs carry enrichment/account/ingest blocks the table never shows —
        # fetching + serializing them is what made this read look slow, not the
        # indexed query itself.
        proj = {
            "transaction_date": 1, "credit_debit_indicator": 1, "amount": 1,
            "foreign_currency_amount": 1, "description": 1, "is_settled": 1,
            "transfer_submethod": 1, "mcc": 1,
            "account.dp_id": 1, "account.institution_name": 1,
            "enrichment.merchant_normalized": 1, "enrichment.category": 1,
            "enrichment.is_recurring": 1,
        }
        t0 = time.perf_counter()
        docs = await db.transactions.find(q, proj).sort(
            [("transaction_date", -1), ("_id", -1)]).limit(page_size + 1).to_list(None)
        query_ms = round((time.perf_counter() - t0) * 1000, 1)  # pure indexed-query time
        log_query("transactions", "find",
                  {"filter": q, "projection": proj, "sort": {"transaction_date": -1, "_id": -1},
                   "limit": page_size + 1}, query_ms, result=docs)
        nxt = None
        if len(docs) > page_size:
            docs = docs[:page_size]
            last = docs[-1]
            nxt = f"{last['transaction_date'].isoformat()}|{last['_id']}"
        # latency_ms = DB query only (excludes jsonable serialization), so the
        # badge matches the Query Inspector's number.
        return {"data": jsonable(docs), "next_cursor": nxt, "latency_ms": query_ms}

    async def get_budgets(self, customer_id: str) -> dict:
        db = self.db
        # ONE profile read: budget definitions (pfm_settings) AND the embedded
        # consent boxes, then resolve the pfm scope in-process (scope_from_profile)
        # instead of a separate gate find_one.
        proj = {"pfm_settings": 1, "accounts.account_id": 1,
                "accounts.is_internal": 1, "accounts.consents": 1}
        t0 = time.perf_counter()
        profile = await db.customer_profiles.find_one({"_id": customer_id}, proj)
        log_query("customer_profiles", "find_one",
                  {"filter": {"_id": customer_id}, "projection": proj},
                  (time.perf_counter() - t0) * 1000, result=profile)
        allowed = scope_from_profile(profile, "pfm")
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail={"error": "Consent.InvalidScope",
                        "detail": "no authorized pfm consent in scope"})
        return await compute_budgets(
            db, customer_id, allowed,
            (profile or {}).get("pfm_settings", {}).get("budgets", []))

    async def put_budgets(self, customer_id: str, budgets: list) -> dict:
        db = self.db
        docs = [{"category": b.category,
                 "monthly_limit": Decimal128(str(round(float(b.monthly_limit), 2))),
                 "alert_threshold": float(b.alert_threshold)}
                for b in budgets]
        await db.customer_profiles.update_one(
            {"_id": customer_id}, {"$set": {"pfm_settings.budgets": docs}})
        return {"ok": True, "count": len(docs)}

    async def safe_to_spend(self, customer_id: str) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "one_view")
        profile = await db.customer_profiles.find_one({"_id": customer_id})
        now = datetime.now(timezone.utc)

        cash = Decimal("0")
        commitments: list[dict] = []
        for acc in profile.get("accounts", []):
            if acc["account_id"] not in allowed:
                continue
            bal = acc.get("balances") or {}
            if acc["type"] == "deposit" and acc["subtype"] != "pension" and bal.get("available_balance"):
                cash += bal["available_balance"]["amount"].to_decimal()
            if acc["type"] in ("credit", "loan") and acc.get("minimum_payment_amount"):
                commitments.append({
                    "label": f'{acc["institution_name"]} {acc["account_name"]}',
                    "amount": f'{acc["minimum_payment_amount"]["amount"].to_decimal():.2f}',
                    "due_date": acc.get("payment_due_date"),
                    "kind": "installment" if acc["type"] == "loan" else "card_min_payment",
                })

        uw = await db.uw_features.find_one({"_id": customer_id})
        payday = 28
        if uw:
            days = [m.get("salary_credit_date") for a in uw.get("accounts", [])
                    for m in a.get("monthly", []) if m.get("salary_credit_date")]
            if days:
                payday = sorted(days)[len(days) // 2]
        next_payday = now.replace(day=min(payday, 28)) if now.day < payday \
            else (now.replace(day=1) + timedelta(days=32)).replace(day=min(payday, 28))

        # Same scope LIST + same pipeline as the Recurring card → the Query
        # Inspector dedupes the two reads into one card (and it's one DB query
        # shape to reason about). Don't collapse to a set: order must match.
        pfm_allowed = await require_scope(db, customer_id, "pfm")
        rec = await aggregate_list(db.transactions,
                                   recurring_groups_pipeline(customer_id, pfm_allowed))
        upcoming = Decimal("0")
        for g in rec:
            if g["category"] == "salary_income":
                continue
            nxt = g["last_date"].replace(tzinfo=timezone.utc) + timedelta(
                days=30 if g["period"] == "monthly" else 7)
            if now <= nxt <= next_payday:
                upcoming += g["amount"].to_decimal()
                commitments.append({"label": g["merchant"] or "recurring payment",
                                    "amount": f'{g["amount"].to_decimal():.2f}',
                                    "due_date": nxt.date().isoformat(), "kind": "recurring"})

        committed = upcoming + sum(Decimal(c["amount"]) for c in commitments
                                   if c["kind"] != "recurring" and c["due_date"]
                                   and datetime.fromisoformat(c["due_date"]).replace(tzinfo=timezone.utc) <= next_payday)
        return {"available_cash": f"{cash:.2f}",
                "committed_before_payday": f"{committed:.2f}",
                "safe_to_spend": f"{cash - committed:.2f}",
                "next_payday": next_payday.date().isoformat(),
                "commitments": commitments}

    async def commitments_calendar(self, customer_id: str) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "one_view")
        profile = await db.customer_profiles.find_one({"_id": customer_id})
        items = []
        for acc in profile.get("accounts", []):
            if acc["account_id"] not in allowed or acc["type"] not in ("credit", "loan"):
                continue
            if acc.get("payment_due_date") and acc.get("minimum_payment_amount"):
                items.append({"date": acc["payment_due_date"],
                              "label": f'{acc["institution_name"]} — {acc["account_name"]}',
                              "amount": f'{acc["minimum_payment_amount"]["amount"].to_decimal():.2f}',
                              "kind": "installment" if acc["type"] == "loan" else "card_payment"})
        try:
            rec = await self.recurring(customer_id)
            for g in rec["groups"]:
                if not g["zombie"] and g["category"] != "salary_income":
                    items.append({"date": g["next_expected"], "label": g["label"],
                                  "amount": g["amount"], "kind": "recurring"})
        except Exception:
            pass
        return {"items": sorted(items, key=lambda x: x["date"])}

    async def credit_utilization(self, customer_id: str) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "one_view")
        profile = await db.customer_profiles.find_one({"_id": customer_id})
        cards, total_limit, total_owed = [], Decimal("0"), Decimal("0")
        for acc in profile.get("accounts", []):
            if acc["account_id"] not in allowed or acc["type"] != "credit" or not acc.get("limit"):
                continue
            limit = acc["limit"]["amount"].to_decimal()
            bal = (acc.get("balances") or {}).get("current_balance")
            owed = bal["amount"].to_decimal() if bal and bal["credit_debit_indicator"] == "debit" else Decimal("0")
            total_limit += limit
            total_owed += owed
            cards.append({"institution": acc["institution_name"], "name": acc["account_name"],
                          "masked": acc["account_number_masked"], "limit": f"{limit:.2f}",
                          "owed": f"{owed:.2f}",
                          "utilization": round(float(owed / limit), 3) if limit else None})
        return {"cards": cards, "portfolio_limit": f"{total_limit:.2f}",
                "portfolio_owed": f"{total_owed:.2f}",
                "portfolio_utilization": round(float(total_owed / total_limit), 3) if total_limit else None}

    async def money_map(self, customer_id: str, month: str | None = None) -> dict:
        db = self.db
        allowed = await require_scope(db, customer_id, "pfm")
        month = month or month_now()
        facets = await aggregate_list(db.transactions, [
            {"$match": {"customer_id": customer_id, "account.account_id": {"$in": allowed},
                        "enrichment.month": month,
                        "enrichment.is_transfer_own_account": False}},
            {"$facet": {
                "income": [
                    {"$match": {"credit_debit_indicator": "credit"}},
                    {"$group": {"_id": {"cat": "$enrichment.category",
                                        "inst": "$account.institution_name"},
                                "total": {"$sum": "$amount.amount"}}},
                ],
                "spending": [
                    {"$match": {"credit_debit_indicator": "debit"}},
                    {"$group": {"_id": {"cat": "$enrichment.category",
                                        "inst": "$account.institution_name"},
                                "total": {"$sum": "$amount.amount"}}},
                ],
            }},
        ])
        return jsonable(facets[0])
