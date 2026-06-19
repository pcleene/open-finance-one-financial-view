"""Compose a transaction's embedding input from (most of) its own fields
(brief §5) — no stored concatenated field. Built inline at embed time for the
small capped sample, so embedding richly costs nothing extra and we never
bloat the ~400M production rows.

Amount is rendered descriptively (formatted + direction + qualitative bucket):
embedding models match meaning, not arithmetic — "RM 1,234.56 · money out ·
large" carries intent; precise numeric ranges stay on the structured filter.
Text composition mirrors NPF's generate_member_embedding_text.
"""

from __future__ import annotations


def _amount_phrase(txn: dict) -> str:
    amt = txn.get("amount") or {}
    cur = amt.get("currency") or "MYR"
    try:
        val = float(str(amt.get("amount")))
    except (TypeError, ValueError):
        val = 0.0
    direction = "money in" if txn.get("credit_debit_indicator") == "credit" else "money out"
    bucket = (txn.get("enrichment") or {}).get("amount_bucket")
    phrase = f"{cur} {val:,.2f} · {direction}"
    return phrase + (f" · {bucket}" if bucket else "")


def compose_embedding_text(txn: dict) -> str:
    enr = txn.get("enrichment") or {}
    acc = txn.get("account") or {}
    parts: list[str] = []

    if txn.get("description"):
        parts.append(str(txn["description"]))
    if enr.get("merchant_normalized"):
        parts.append(f"Merchant: {enr['merchant_normalized']}")
    if enr.get("category"):
        cat = enr["category"]
        parts.append(f"Category: {cat} / {enr['subcategory']}" if enr.get("subcategory")
                     else f"Category: {cat}")
    if enr.get("channel"):
        parts.append(f"Channel: {enr['channel']}")
    if txn.get("transfer_submethod"):
        parts.append(f"Method: {txn['transfer_submethod']}")
    parts.append(_amount_phrase(txn))
    if acc.get("institution_name"):
        parts.append(f"Bank: {acc['institution_name']}")
    if acc.get("type"):
        parts.append(f"Account: {acc['type']}/{acc.get('subtype', '')}".rstrip("/"))
    if txn.get("recipient_reference"):
        parts.append(f"Ref: {txn['recipient_reference']}")
    fx = txn.get("foreign_currency_amount")
    if fx:
        parts.append(f"FX: {fx.get('currency')} {fx.get('amount')}")
    if enr.get("tags"):
        parts.append("Tags: " + ", ".join(str(t) for t in enr["tags"]))
    if enr.get("is_recurring"):
        parts.append("recurring")
    if txn.get("transaction_date"):
        parts.append(f"Date: {str(txn['transaction_date'])[:10]}")

    return " · ".join(p for p in parts if p)
