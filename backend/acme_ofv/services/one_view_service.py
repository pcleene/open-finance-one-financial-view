"""One View service (brief §5) — Path A: the whole cross-institution position in
ONE indexed read, consent filtering done by $filter on the embedded boxes;
recent activity embedded in the profile and consent-filtered in the same
pipeline (brief §3), so it's a single document read.

Relocated from the former api/app.py one_view + customers handlers."""

import time
from datetime import datetime, timezone
from decimal import Decimal

from bson import Decimal128

from acme_ofv.api.serialize import jsonable
from acme_ofv.consent.gate import one_view_pipeline, scope_from_profile
from acme_ofv.db import aggregate_list
from acme_ofv.services.pfm_service import compute_budgets


def _live_summary(accounts: list[dict], base: dict | None) -> dict:
    """Headline figures recomputed from the consent-filtered accounts THIS read
    returned, so the net position / institution / account / consent counts always
    match the cards on screen. The materialized `summary` is maintained async by
    refresh_summary (and still used by the customer switcher); recomputing here
    removes the window between a consent box-flip and that async write where the
    list would update but the header would lag."""
    now = datetime.now(timezone.utc)
    net = Decimal("0")
    institutions: list[str] = []
    external_count = 0
    consent_ids: set[str] = set()
    for acc in accounts:
        if not acc.get("is_internal"):
            external_count += 1
            for c in acc.get("consents", []) or []:
                exp = c.get("expiration_datetime")
                if exp is not None and getattr(exp, "tzinfo", None) is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if c.get("status") == "authorized" and (exp is None or exp > now):
                    consent_ids.add(c["consent_id"])
        name = "Acme" if acc.get("is_internal") else acc.get("institution_name")
        if name and name not in institutions:
            institutions.append(name)
        bal = (acc.get("balances") or {}).get("current_balance")
        if bal and bal.get("amount") is not None:
            v = bal["amount"].to_decimal()
            net += v if bal.get("credit_debit_indicator") == "credit" else -v
    summary = dict(base or {})
    summary["net_position"] = {"amount": Decimal128(net.quantize(Decimal("0.01"))),
                               "currency": "MYR"}
    summary["institutions_linked"] = institutions
    summary["external_account_count"] = external_count
    summary["active_consent_count"] = len(consent_ids)
    return summary


class OneViewService:
    def __init__(self, db):
        self.db = db

    async def one_view(self, customer_id: str) -> dict | None:
        db = self.db
        t0 = time.perf_counter()
        rows = await aggregate_list(db.customer_profiles,
                                    one_view_pipeline(customer_id, "one_view"))
        t_profile = (time.perf_counter() - t0) * 1000
        if not rows:
            return None
        profile = rows[0]
        recent = profile.pop("recent_transactions", [])
        # header recomputed from the same consent-filtered accounts → always
        # consistent with the cards, even mid-revoke before refresh_summary lands
        profile["summary"] = _live_summary(profile.get("accounts", []),
                                            profile.get("summary"))
        # Budget alerts are served from THIS one profile read (the One View
        # aggregate already returns pfm_settings + the consent-filtered accounts):
        # derive the pfm scope in-process and run a single transactions aggregate.
        # No second customer_profiles read — the home page is one profile read.
        budgets = await compute_budgets(
            db, customer_id, scope_from_profile(profile, "pfm"),
            (profile.get("pfm_settings") or {}).get("budgets", []))
        return {
            "profile": jsonable(profile),
            "recent_activity": jsonable(recent),
            "budgets": budgets,
            "latency_ms": {"profile_read": round(t_profile, 2)},
            "reads": {"profile": 1},
        }

    async def list_customers(self) -> dict:
        docs = await self.db.customer_profiles.find(
            {}, {"customer.full_name": 1, "customer.preferred_name": 1,
                 "customer.segment": 1, "summary": 1},
        ).sort("_id", 1).limit(30).to_list(None)
        out = []
        for d in docs:
            summary = d.get("summary", {})
            cust = d["customer"]
            out.append({
                "customer_id": d["_id"],
                "full_name": cust["full_name"],
                "preferred_name": cust.get("preferred_name", cust["full_name"].split()[0]),
                "segment": cust["segment"],
                "institutions": summary.get("institutions_linked", []),
                "external_account_count": summary.get("external_account_count", 0),
                "active_consent_count": summary.get("active_consent_count", 0),
            })
        return {"customers": out}
