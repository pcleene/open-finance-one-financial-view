"""Consolidated `customer_profiles` writes (brief §3): consent boxes projected
onto embedded accounts, the capped recent-transactions embed that makes One View
a single read, and the denormalized `summary` recomputed after every sync/flip."""

from datetime import datetime, timezone
from decimal import Decimal

from bson import Decimal128

RECENT_CAP = 15  # capped recent_transactions embedded in the profile (brief §3)


async def build_consent_boxes(db, account_id: str) -> list[dict]:
    """One box per purpose for this account. Prefer the currently-AUTHORIZED
    (non-expired) consent for that purpose; otherwise fall back to the highest
    `_rcp_version` (latest state, e.g. revoked/expired).

    Why not just "highest version wins": the duplicate rule revokes a predecessor
    AT re-link time, so a superseded/revoked consent can carry a HIGHER
    `_rcp_version` than the authorized re-link. Picking purely by version would
    then stamp the box `revoked` and hide the account from every read even though
    an authorized consent exists. Preferring the authorized consent fixes that
    while still showing the latest state when nothing is authorized."""
    now = datetime.now(timezone.utc)
    chosen: dict[str, dict] = {}
    chosen_authz: dict[str, bool] = {}
    # DESC → the first consent seen per purpose is the highest-version fallback
    cursor = db.consents.find({"accounts.account_id": account_id}).sort("_rcp_version", -1)
    async for c in cursor:
        purpose = c["consent_purpose"]
        exp = c["expiration_datetime"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        authz = c["status"] == "authorized" and exp > now
        if purpose not in chosen:
            chosen[purpose], chosen_authz[purpose] = c, authz
        elif authz and not chosen_authz[purpose]:
            # a lower-version but currently-authorized consent beats the
            # higher-version inactive one we picked first
            chosen[purpose], chosen_authz[purpose] = c, True
    return [
        {
            "consent_id": c["consent_id"],
            "consent_purpose": purpose,
            "internal_purposes": c.get("internal_purposes", []),
            "permissions": c["permissions"],
            "status": c["status"],
            "status_reason": c.get("status_reason"),
            "expiration_datetime": c["expiration_datetime"],
            "updated_by": c.get("updated_by"),
            "_rcp_version": c.get("_rcp_version", 0),
        }
        for purpose, c in chosen.items()
    ]


async def upsert_embedded_account(db, customer_id: str, embedded: dict) -> None:
    res = await db.customer_profiles.update_one(
        {"_id": customer_id, "accounts.account_id": embedded["account_id"]},
        {"$set": {"accounts.$": embedded}},
    )
    if res.matched_count == 0:
        await db.customer_profiles.update_one(
            {"_id": customer_id}, {"$push": {"accounts": embedded}})


async def recent_transactions(db, customer_id: str, cap: int = RECENT_CAP) -> list[dict]:
    """Compact most-recent rows embedded in the profile so One View is a single
    read (brief §3). Stored across all of the customer's (non-erased) accounts;
    the one-view pipeline $filters them to the consented set at read time, and
    erased-account rows are already gone from the transactions collection."""
    docs = await db.transactions.find(
        {"customer_id": customer_id},
        {"transaction_id": 1, "transaction_date": 1, "amount": 1,
         "credit_debit_indicator": 1, "description": 1, "is_settled": 1,
         "account.account_id": 1, "account.dp_id": 1, "account.institution_name": 1,
         "enrichment.merchant_normalized": 1, "enrichment.category": 1},
    ).sort([("transaction_date", -1), ("_id", -1)]).limit(cap).to_list(None)
    out = []
    for d in docs:
        acc = d.get("account") or {}
        enr = d.get("enrichment") or {}
        out.append({
            "transaction_id": d.get("transaction_id"),
            "transaction_date": d.get("transaction_date"),
            "amount": d.get("amount"),
            "credit_debit_indicator": d.get("credit_debit_indicator"),
            "description": d.get("description"),
            "is_settled": d.get("is_settled"),
            "account": {"account_id": acc.get("account_id"), "dp_id": acc.get("dp_id"),
                        "institution_name": acc.get("institution_name")},
            "enrichment": {"merchant_normalized": enr.get("merchant_normalized"),
                           "category": enr.get("category")},
        })
    return out


async def refresh_summary(db, customer_id: str) -> None:
    """Denormalized one-read conveniences — recomputed after every sync/flip."""
    profile = await db.customer_profiles.find_one({"_id": customer_id})
    if not profile:
        return
    now = datetime.now(timezone.utc)
    net = Decimal("0")
    institutions: list[str] = []
    external_count = 0
    consent_ids: set[str] = set()
    for acc in profile.get("accounts", []):
        authorized = [c for c in acc.get("consents", [])
                      if c["status"] == "authorized"
                      and c["expiration_datetime"].replace(tzinfo=timezone.utc) > now]
        visible = acc.get("is_internal") or authorized
        if not visible:
            continue
        if not acc.get("is_internal"):
            external_count += 1
            consent_ids.update(c["consent_id"] for c in authorized)
        name = "Acme" if acc.get("is_internal") else acc["institution_name"]
        if name not in institutions:
            institutions.append(name)
        bal = (acc.get("balances") or {}).get("current_balance")
        if bal:
            v = bal["amount"].to_decimal()
            net += v if bal["credit_debit_indicator"] == "credit" else -v
    recent = await recent_transactions(db, customer_id)
    await db.customer_profiles.update_one(
        {"_id": customer_id},
        {"$set": {
            "summary.net_position": {"amount": Decimal128(net.quantize(Decimal("0.01"))),
                                     "currency": "MYR"},
            "summary.institutions_linked": institutions,
            "summary.external_account_count": external_count,
            "summary.active_consent_count": len(consent_ids),
            "summary.last_refreshed_at": now,
            "audit.updated_at": now,
            "recent_transactions": recent,
        }},
    )
