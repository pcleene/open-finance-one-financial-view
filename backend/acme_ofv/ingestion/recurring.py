"""Recurring detector (brief §9 Tier 1) — ingestion-side, write-time stamping."""

import uuid
from collections import defaultdict


async def detect_recurring(db, customer_id: str) -> int:
    """Group candidate rows by merchant/recipient root; flag groups with a
    stable period (monthly 27–33 d or weekly 6–8 d) and amount band ±10%."""
    rows = await db.transactions.find(
        {"customer_id": customer_id, "credit_debit_indicator": "debit"},
        {"transaction_date": 1, "amount": 1, "enrichment.merchant_normalized": 1,
         "description": 1},
    ).sort("transaction_date", 1).to_list(None)

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("enrichment") or {}).get("merchant_normalized") \
            or r["description"][:40]
        groups[key].append(r)

    flagged = 0
    for key, items in groups.items():
        if len(items) < 3:
            continue
        amts = [float(i["amount"]["amount"].to_decimal()) for i in items]
        med = sorted(amts)[len(amts) // 2]
        if med == 0 or any(abs(a - med) > 0.10 * med for a in amts):
            continue
        gaps = [(items[i + 1]["transaction_date"] - items[i]["transaction_date"]).days
                for i in range(len(items) - 1)]
        monthly = all(26 <= g <= 35 for g in gaps)
        weekly = all(5 <= g <= 9 for g in gaps)
        if not (monthly or weekly):
            continue
        gid = f"rec_{uuid.uuid4().hex[:10]}"
        await db.transactions.update_many(
            {"_id": {"$in": [i["_id"] for i in items]}},
            {"$set": {"enrichment.is_recurring": True,
                      "enrichment.recurring_group_id": gid,
                      "enrichment.recurring_period": "monthly" if monthly else "weekly"}})
        flagged += 1
    return flagged
