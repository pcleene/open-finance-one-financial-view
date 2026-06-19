"""Credit underwriting service (brief §10) — live feature store rolled up at
read time over the consented account set ONLY, score computed inside an
aggregation pipeline, decisions persisted as immutable consent-stamped runs.

Relocated verbatim from the former api/underwriting.py (behavior-preserving)."""

import time
from datetime import datetime, timedelta, timezone

from acme_ofv.api.serialize import jsonable
from acme_ofv.consent.gate import require_scope, resolve_consent_scope
from acme_ofv.db import aggregate_list
from acme_ofv.query_log import get_log

MODEL_VERSION = "poc-logit-v1"


def score_pipeline(customer_id: str, scope: list[str], derived: dict) -> list[dict]:
    """One aggregation over uw_features: $filter to consented scope → $facet
    rollups → weighted score → $switch band/decision. computed_in_db: true."""
    salary = derived["median_monthly_salary"] or 0.0
    regularity = derived["salary_regularity_score"]
    return [
        {"$match": {"_id": customer_id}},
        {"$project": {"scoped": {"$filter": {
            "input": "$accounts", "as": "a",
            "cond": {"$in": ["$$a.account_id", scope]}}}}},
        {"$facet": {
            "flows": [
                {"$unwind": "$scoped"}, {"$unwind": "$scoped.monthly"},
                {"$group": {"_id": None,
                            "inflow": {"$sum": {"$toDouble": "$scoped.monthly.inflow"}},
                            "outflow": {"$sum": {"$toDouble": "$scoped.monthly.outflow"}},
                            "gambling_6m": {"$sum": {"$toDouble": "$scoped.monthly.gambling_spend"}},
                            "months": {"$addToSet": "$scoped.monthly.month"}}},
            ],
            "credit": [
                {"$unwind": "$scoped"},
                {"$match": {"scoped.credit": {"$exists": True}}},
                {"$group": {"_id": None,
                            "total_limit": {"$sum": {"$toDouble": "$scoped.credit.limit"}},
                            "total_owed": {"$sum": {"$toDouble": "$scoped.credit.current_owed"}},
                            "card_min_payments": {"$sum": {"$toDouble": "$scoped.credit.minimum_payment_amount"}}}},
            ],
            "loans": [
                {"$unwind": "$scoped"},
                {"$match": {"scoped.loan": {"$exists": True}}},
                {"$group": {"_id": None,
                            "installments": {"$sum": {"$toDouble": "$scoped.loan.installment"}}}},
            ],
            "liquidity": [
                {"$unwind": "$scoped"},
                {"$match": {"scoped.balance_stats_90d": {"$exists": True}}},
                {"$group": {"_id": None,
                            "avg_eod": {"$avg": {"$toDouble": "$scoped.balance_stats_90d.avg_eod"}},
                            "min_eod": {"$min": {"$toDouble": "$scoped.balance_stats_90d.min_eod"}},
                            "days_below_500": {"$max": "$scoped.balance_stats_90d.days_below_500"}}},
            ],
        }},
        {"$project": {
            "rollup": {
                "months_in_window": {"$size": {"$ifNull": [{"$first": "$flows.months"}, []]}},
                "total_inflow": {"$ifNull": [{"$first": "$flows.inflow"}, 0]},
                "total_outflow": {"$ifNull": [{"$first": "$flows.outflow"}, 0]},
                "gambling_6m": {"$ifNull": [{"$first": "$flows.gambling_6m"}, 0]},
                "portfolio_limit": {"$ifNull": [{"$first": "$credit.total_limit"}, 0]},
                "portfolio_owed": {"$ifNull": [{"$first": "$credit.total_owed"}, 0]},
                "card_min_payments": {"$ifNull": [{"$first": "$credit.card_min_payments"}, 0]},
                "loan_installments": {"$ifNull": [{"$first": "$loans.installments"}, 0]},
                "avg_eod": {"$ifNull": [{"$first": "$liquidity.avg_eod"}, 0]},
                "min_eod": {"$ifNull": [{"$first": "$liquidity.min_eod"}, 0]},
                "days_below_500": {"$ifNull": [{"$first": "$liquidity.days_below_500"}, 0]},
            },
        }},
        {"$addFields": {
            "rollup.utilization_now": {"$cond": [
                {"$gt": ["$rollup.portfolio_limit", 0]},
                {"$round": [{"$divide": ["$rollup.portfolio_owed", "$rollup.portfolio_limit"]}, 4]}, 0]},
            "rollup.dsr_estimate": {"$cond": [
                {"$gt": [salary, 0]},
                {"$round": [{"$divide": [
                    {"$add": ["$rollup.loan_installments", "$rollup.card_min_payments"]},
                    salary]}, 4]}, 1.0]},
            "rollup.net_flow": {"$subtract": ["$rollup.total_inflow", "$rollup.total_outflow"]},
            "rollup.median_monthly_salary": salary,
            "rollup.salary_regularity_score": regularity,
        }},
        {"$addFields": {
            "score": {"$round": [{"$add": [
                600,
                {"$multiply": [85, regularity]},
                {"$multiply": [70, {"$min": [{"$divide": ["$rollup.avg_eod", 5000]}, 1]}]},
                {"$cond": [{"$gt": ["$rollup.net_flow", 0]}, 25, -10]},
                {"$multiply": [-85, "$rollup.utilization_now"]},
                {"$multiply": [-110, {"$min": ["$rollup.dsr_estimate", 1]}]},
                {"$multiply": [-90, {"$min": [{"$divide": ["$rollup.gambling_6m", 1000]}, 1]}]},
                {"$cond": [{"$gt": ["$rollup.days_below_500", 10]}, -25, 0]},
            ]}, 0]},
        }},
        {"$addFields": {
            "band": {"$switch": {"branches": [
                {"case": {"$gte": ["$score", 740]}, "then": "A"},
                {"case": {"$gte": ["$score", 700]}, "then": "B+"},
                {"case": {"$gte": ["$score", 660]}, "then": "B"},
                {"case": {"$gte": ["$score", 620]}, "then": "C+"},
                {"case": {"$gte": ["$score", 580]}, "then": "C"},
            ], "default": "D"}},
            "decision": {"$switch": {"branches": [
                {"case": {"$gte": ["$score", 700]}, "then": "approve"},
                {"case": {"$gte": ["$score", 640]}, "then": "approve_with_conditions"},
                {"case": {"$gte": ["$score", 580]}, "then": "manual_review"},
            ], "default": "decline"}},
            "reason_codes": {"$concatArrays": [
                {"$cond": [{"$gte": ["$rollup.utilization_now", 0.5]}, ["UTIL_HIGH"], []]},
                {"$cond": [{"$lt": ["$rollup.dsr_estimate", 0.4]}, ["DSR_OK"], ["DSR_HIGH"]]},
                {"$cond": [{"$gte": [regularity, 0.9]}, ["INCOME_STABLE"], ["INCOME_VARIABLE"]]},
                {"$cond": [{"$gt": ["$rollup.gambling_6m", 0]}, ["GAMBLING_ACTIVITY"], []]},
                {"$cond": [{"$lt": ["$rollup.min_eod", 500]}, ["LOW_LIQUIDITY_BUFFER"], []]},
                {"$cond": [{"$gt": ["$rollup.net_flow", 0]}, ["NET_FLOW_POSITIVE"], ["NET_FLOW_NEGATIVE"]]},
            ]},
        }},
    ]


class UnderwritingService:
    def __init__(self, db):
        self.db = db

    async def _salary_stats(self, customer_id: str, scope: list[str]) -> dict:
        rows = await aggregate_list(self.db.transactions, [
            {"$match": {"customer_id": customer_id,
                        "account.account_id": {"$in": scope},
                        "enrichment.category": "salary_income",
                        "credit_debit_indicator": "credit",
                        "transaction_date": {"$gte": datetime.now(timezone.utc) - timedelta(days=185)}}},
            {"$group": {"_id": "$enrichment.month",
                        "total": {"$sum": {"$toDouble": "$amount.amount"}},
                        "day": {"$max": {"$dayOfMonth": "$transaction_date"}}}},
            {"$group": {"_id": None,
                        "median_salary": {"$median": {"input": "$total", "method": "approximate"}},
                        "mean": {"$avg": "$total"},
                        "stddev": {"$stdDevPop": "$total"},
                        "months_observed": {"$sum": 1},
                        "payday": {"$median": {"input": "$day", "method": "approximate"}}}},
        ])
        if not rows or not rows[0]["months_observed"]:
            return {"salary_detected": False, "median_monthly_salary": 0.0,
                    "salary_regularity_score": 0.0, "payday_day_of_month": None,
                    "months_observed": 0}
        r = rows[0]
        regularity = max(0.0, 1.0 - (r["stddev"] or 0.0) / r["mean"]) if r["mean"] else 0.0
        return {"salary_detected": True,
                "median_monthly_salary": round(r["median_salary"], 2),
                "salary_regularity_score": round(min(1.0, regularity + 0.04 * (r["months_observed"] >= 5)), 3),
                "payday_day_of_month": int(r["payday"]),
                "months_observed": r["months_observed"]}

    async def run(self, customer_id: str, product: str = "personal_loan_50k_60m") -> dict:
        db = self.db
        # snapshot the Query Inspector log index at each step boundary so the
        # progress popup can attribute the *real* MongoDB ops to each operation.
        t0 = time.perf_counter(); i0 = len(get_log())
        scope = await require_scope(db, customer_id, "credit_underwriting")  # 403 if ∅
        t_gate = time.perf_counter(); i_gate = len(get_log())

        # Build the feature store on demand at inquiry (brief §10): reactive
        # scoring — the per-account components are computed + persisted exactly
        # when a loan inquiry runs, not nightly for every customer.
        from acme_ofv.ingestion.service import rebuild_uw_features
        await rebuild_uw_features(db, customer_id)
        t_build = time.perf_counter(); i_build = len(get_log())

        derived = await self._salary_stats(customer_id, scope)
        t_features = time.perf_counter(); i_features = len(get_log())

        rows = await aggregate_list(db.uw_features, score_pipeline(customer_id, scope, derived))
        t_score = time.perf_counter(); i_score = len(get_log())
        if not rows:
            return {"error": "feature store empty for customer — run a sync first"}
        result = rows[0]

        now = datetime.now(timezone.utc)
        consent_snapshot = await db.consents.find({
            "customer_id": customer_id, "consent_purpose": "credit_underwriting",
            "status": "authorized", "expiration_datetime": {"$gt": now},
        }).to_list(None)
        uw = await db.uw_features.find_one({"_id": customer_id})
        components = [a for a in (uw or {}).get("accounts", []) if a["account_id"] in scope]
        t_assemble = time.perf_counter()

        latency = {
            "gate": round((t_gate - t0) * 1000, 1),
            "features_build": round((t_build - t_gate) * 1000, 1),
            "features": round((t_features - t_build) * 1000, 1),
            "score": round((t_score - t_features) * 1000, 1),
            "persist": round((t_assemble - t_score) * 1000, 1),
            "total": round((time.perf_counter() - t0) * 1000, 1),
        }
        operations = self._build_operations(
            get_log(), i0, i_gate, i_build, i_features, i_score, latency)

        run_doc = {
            "customer_id": customer_id,
            "run_at": now,
            "requested_product": product,
            "consent_snapshot": consent_snapshot,
            "scope_account_ids": scope,
            "features_snapshot": {"components": components, "rollup": result["rollup"],
                                  "derived": derived},
            "scorecard": {
                "model_version": MODEL_VERSION,
                "score": int(result["score"]),
                "band": result["band"],
                "decision": result["decision"],
                "reason_codes": result["reason_codes"],
                "computed_in_db": True,
            },
            "latency_ms": latency,
            "operations": operations,
        }
        ins = await db.underwriting_runs.insert_one(run_doc)
        run_doc["_id"] = ins.inserted_id
        return jsonable(run_doc)

    @staticmethod
    def _op(key: str, label: str, detail: str, ms, chunk: list) -> dict:
        """One operation: summary + the real MongoDB ops it ran (pipeline + sample
        result), embedded so the underwriting popup can drill into each step and
        historical runs keep their own inspectable trail."""
        return {
            "key": key, "label": label, "detail": detail, "ms": ms,
            "query_count": len(chunk),
            "result_docs": sum(int(e.get("result_count", 0) or 0) for e in chunk),
            "collections": sorted({e["collection"] for e in chunk}),
            "queries": chunk,
        }

    def _build_operations(self, entries, i0, i_gate, i_build, i_features,
                          i_score, latency) -> list[dict]:
        """Ordered, persisted operation log for the underwriting progress popup —
        each step carries its real latency and the MongoDB ops it ran."""
        persist = self._op(
            "persist_run", "Persist immutable run",
            "Snapshot the governing consents + scoped feature components and append "
            "the consent-stamped underwriting_runs record.",
            latency["persist"], entries[i_score:])
        if not persist["collections"]:
            persist["collections"] = ["underwriting_runs"]
        return [
            self._op("consent_gate", "Consent gate",
                     "Resolve the credit_underwriting scope to the consented account "
                     "set — refuse at the read path if it resolves to ∅ (HTTP 403).",
                     latency["gate"], entries[i0:i_gate]),
            self._op("build_feature_store", "Build feature store (on demand)",
                     "Reactive scoring: roll up 6-month per-account components from "
                     "transactions + the reconstructed balance snapshots and persist "
                     "uw_features.",
                     latency["features_build"], entries[i_gate:i_build]),
            self._op("salary_statistics", "Salary statistics",
                     "Median monthly salary, regularity score and payday from the "
                     "consented salary_income credits.",
                     latency["features"], entries[i_build:i_features]),
            self._op("score_in_aggregation", "Score inside aggregation",
                     "Weighted scorecard, band and decision computed entirely in a "
                     "$facet pipeline over uw_features (computed_in_db).",
                     latency["score"], entries[i_features:i_score]),
            persist,
        ]

    async def runs(self, customer_id: str) -> dict:
        docs = await self.db.underwriting_runs.find({"customer_id": customer_id}) \
            .sort("run_at", -1).limit(20).to_list(None)
        return {"runs": jsonable(docs)}

    async def features(self, customer_id: str) -> dict:
        db = self.db
        uw = await db.uw_features.find_one({"_id": customer_id})
        cu_scope = await resolve_consent_scope(db, customer_id, "credit_underwriting")
        pfm_scope = await resolve_consent_scope(db, customer_id, "pfm")
        coverage = []
        for a in (uw or {}).get("accounts", []):
            coverage.append({
                "account_id": a["account_id"], "dp_id": a["dp_id"],
                "type": a["type"], "subtype": a["subtype"],
                "in_credit_underwriting_scope": a["account_id"] in cu_scope,
                "in_pfm_scope": a["account_id"] in pfm_scope,
            })
        return {"features": jsonable(uw), "_consent_coverage": coverage}
