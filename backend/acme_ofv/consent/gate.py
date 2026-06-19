"""The consent gate (brief §5.2) — two enforcement paths, same semantics.

Path A (one-view): enforcement INSIDE the single profile read via $filter on
the embedded consent boxes — zero pre-query lookups, zero joins.

Path B (transaction-level reads): resolve-then-constrain — resolve the allowed
account set from the same embedded boxes, then every downstream query carries
{"account.account_id": {"$in": allowed}}.

Pure read-side: revocation/suspension/expiry/scope changes take effect on the
next read because the state lives where the query runs. The $gt on
expiration_datetime makes EOD expiry self-enforcing at 00:00:00.
"""

import time
from datetime import datetime, timezone

from fastapi import HTTPException

from acme_ofv.consent.purpose_map import (
    INTERNAL_DATA_IN_SCOPE,
    INTERNAL_TO_OFP_PURPOSE,
    PERMISSION_REQUIRED,
)
from acme_ofv.query_log import log_query


def one_view_pipeline(customer_id: str, internal_purpose: str = "one_view",
                      now: datetime | None = None) -> list[dict]:
    """Path A — single-read profile pipeline with consent boxes filtered in-DB."""
    ofp_purposes = INTERNAL_TO_OFP_PURPOSE[internal_purpose]
    permission = PERMISSION_REQUIRED[internal_purpose]
    now = now or datetime.now(timezone.utc)
    box_clause = {"$gt": [{"$size": {"$filter": {
        "input": {"$ifNull": ["$$acc.consents", []]}, "as": "c",
        "cond": {"$and": [
            {"$eq": ["$$c.status", "authorized"]},
            {"$in": ["$$c.consent_purpose", ofp_purposes]},  # AXIS A
            {"$in": [permission, "$$c.permissions"]},
            {"$gt": ["$$c.expiration_datetime", now]},          # EOD expiry
        ]}}}}, 0]}                                              # AXIS B
    clauses = [box_clause]
    if INTERNAL_DATA_IN_SCOPE[internal_purpose]:
        # Acme's own data — not OFP-governed (purpose-dependent, see purpose_map)
        clauses.insert(0, {"$eq": ["$$acc.is_internal", True]})
    return [
        {"$match": {"_id": customer_id}},
        {"$project": {
            "customer": 1, "summary": 1, "pfm_settings": 1, "schema_version": 1,
            "accounts": {"$filter": {
                "input": "$accounts", "as": "acc",
                "cond": {"$or": clauses},
            }},
            "recent_transactions": {"$ifNull": ["$recent_transactions", []]},
        }},
        # consent enforced inside the read: keep only embedded recent rows whose
        # account survived the consent $filter above (brief §3, Path A)
        {"$addFields": {
            "recent_transactions": {"$filter": {
                "input": "$recent_transactions", "as": "t",
                "cond": {"$in": ["$$t.account.account_id", "$accounts.account_id"]},
            }},
        }},
    ]


def scope_from_profile(profile: dict | None, internal_purpose: str,
                       now: datetime | None = None) -> list[str]:
    """Pure Path-B scope resolution from an ALREADY-fetched profile (no DB read).

    Lets a handler that already reads `customer_profiles` for its own reasons
    (e.g. budgets, which also needs `pfm_settings`) gate WITHOUT a second
    `find_one` — same embedded boxes, same purpose×permission×expiry test."""
    if not profile:
        return []
    ofp_purposes = INTERNAL_TO_OFP_PURPOSE[internal_purpose]
    permission = PERMISSION_REQUIRED[internal_purpose]
    internal_ok = INTERNAL_DATA_IN_SCOPE[internal_purpose]
    now = now or datetime.now(timezone.utc)
    return [
        acc["account_id"] for acc in profile.get("accounts", [])
        if (acc.get("is_internal") and internal_ok) or any(
            c["status"] == "authorized"
            and c["consent_purpose"] in ofp_purposes
            and permission in c["permissions"]
            and c["expiration_datetime"].replace(tzinfo=timezone.utc) > now
            for c in acc.get("consents", [])
        )
    ]


async def resolve_consent_scope(db, customer_id: str, internal_purpose: str) -> list[str]:
    """Path B — account_ids this customer has authorized for this internal purpose, right now.
    Enforces reads against collections that do not embed consent - eg:  at transaction-level (§5.2, Path B)."""
    proj = {"accounts.account_id": 1, "accounts.is_internal": 1, "accounts.consents": 1}
    t0 = time.perf_counter()
    profile = await db.customer_profiles.find_one({"_id": customer_id}, projection=proj)
    # Surface this in the Query Inspector: it's the consent-scope resolution — where
    # the allowed-account set (the $in list every Path-B read carries) comes from.
    # Runs BEFORE the transaction queries, so it shows as step 1.
    log_query("customer_profiles", "find_one",
              {"filter": {"_id": customer_id}, "projection": proj},
              (time.perf_counter() - t0) * 1000, result=profile)
    return scope_from_profile(profile, internal_purpose)


async def require_scope(db, customer_id: str, internal_purpose: str) -> list[str]:
    """Gate dependency body: empty scope for a purpose-gated route is a 403, not thin data."""
    allowed = await resolve_consent_scope(db, customer_id, internal_purpose)
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail={"error": "Consent.InvalidScope",
                    "detail": f"no authorized {internal_purpose} consent in scope"},
        )
    return allowed
