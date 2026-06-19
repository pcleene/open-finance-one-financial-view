"""balance_snapshots time series (brief §3).

the Open Finance platform serves balances point-in-time only. The primary path
(`reconstruct_snapshot_history`) rebuilds the end-of-day series by walking the
real pulled transactions backward from the current-balance anchor;
`synthesize_snapshot_history` is the documented zero-transaction fallback.
"""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from acme_ofv.ingestion.amounts import d128


async def synthesize_snapshot_history(db, customer_id: str, account: dict,
                                      current: Decimal, indicator: str, dp_id: str) -> None:
    """POC fallback: synthesize 90 d of plausible history behind the first real
    snapshot so the net-worth trend is demoable when there are no transactions to
    reconstruct from."""
    existing = await db.balance_snapshots.find_one(
        {"meta.account_id": account["account_id"]})
    if existing:
        return
    rng = random.Random(account["account_id"])
    now = datetime.now(timezone.utc)
    docs = []
    v = float(current)
    for back in range(90):
        day = (now - timedelta(days=back)).replace(hour=23, minute=59, second=0, microsecond=0)
        docs.append({
            "as_of": day,
            "meta": {"customer_id": customer_id, "account_id": account["account_id"],
                     "dp_id": dp_id, "type": account["type"]},
            "current_balance": d128(round(max(v, 0.0), 2)),
            "available_balance": d128(round(max(v, 0.0), 2)),
            "credit_debit_indicator": indicator,
            "currency": "MYR",
        })
        v *= rng.uniform(0.965, 1.04)
    await db.balance_snapshots.insert_many(docs)


async def reconstruct_snapshot_history(db, customer_id: str, account: dict,
                                       anchor: Decimal, indicator: str, dp_id: str,
                                       days: int = 90) -> int:
    """Rebuild the EOD balance time series by walking the *real* pulled
    transactions backward from the point-in-time balance anchor (brief §3).

    On a signed position (credit balance positive, owed balance negative) every
    `credit` row adds its amount and every `debit` row subtracts it, so the
    balance *before* a row is the inverse. Depth is bounded by the pulled
    transaction window; anchor-only if there is nothing to walk back from.
    Returns the number of reconstructed snapshots inserted."""
    account_id = account["account_id"]
    now = datetime.now(timezone.utc)
    # idempotency: a prior backfill already built history for this account
    older = await db.balance_snapshots.find_one(
        {"meta.account_id": account_id, "as_of": {"$lt": now - timedelta(days=1)}})
    if older:
        return 0
    rows = await db.transactions.find(
        {"customer_id": customer_id, "account.account_id": account_id},
        {"transaction_date": 1, "amount": 1, "credit_debit_indicator": 1},
    ).sort("transaction_date", 1).to_list(None)
    if not rows:
        return 0  # nothing to reconstruct from — the anchor snapshot stands alone

    daily_delta: dict = {}
    for r in rows:
        amt = r["amount"]["amount"].to_decimal()
        delta = amt if r["credit_debit_indicator"] == "credit" else -amt
        day = r["transaction_date"].date()
        daily_delta[day] = daily_delta.get(day, Decimal("0")) + delta

    today = now.date()
    running = anchor if indicator == "credit" else -anchor  # signed position @ EOD today
    docs = []
    for back in range(1, days + 1):
        # EOD(today-back) = EOD(today-back+1) − delta(today-back+1)
        running -= daily_delta.get(today - timedelta(days=back - 1), Decimal("0"))
        magnitude = running if running >= 0 else -running
        docs.append({
            "as_of": (now - timedelta(days=back)).replace(
                hour=23, minute=59, second=0, microsecond=0),
            "meta": {"customer_id": customer_id, "account_id": account_id,
                     "dp_id": dp_id, "type": account["type"]},
            "current_balance": d128(magnitude),
            "available_balance": d128(magnitude),
            "credit_debit_indicator": "credit" if running >= 0 else "debit",
            "currency": "MYR",
        })
    await db.balance_snapshots.insert_many(docs)
    return len(docs)
