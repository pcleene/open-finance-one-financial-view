# Demo runbook — Acme One Financial View (brief §14 order)

Pre-flight: `ops/start_all.sh` (or the four processes manually), then open
http://localhost:5174 and MongoDB Compass on the Atlas cluster.

## 1. The profile document
Compass → `acme_ofv.customer_profiles` → `_id: "acme_cust_000001"` (Aisyah).
Point at: embedded accounts (internal + Bank Beta + Bank Gamma + NPF), the **consent
box** on each account (the Open Finance platform vocabulary verbatim: purpose, permissions,
status, EOD expiry), `summary`, `_rcp_version` on boxes.
> "Every account she linked, every balance, and the consent that governs each —
> one document. The MySQL equivalent is a six-table fan-out join per page load."

## 2. One View
Frontend → One View (Aisyah). Latency badge: **1 document read · N ms** —
consent filtering by `$filter` inside the read, zero joins.

## 3. Purpose enforcement
Switch persona to **Farid** (top right). Insights: rich. Underwriting console →
Run scorecard → **403 Consent.InvalidScope** card.
> "Same data, same feature store — the purpose gate is in the read path,
> not in app discipline."

## 4. Live revocation (the money shot)
Switch to **Mei Ling** → Consent Centre → revoke Bank Gamma **pfm** → back to One View:
Bank Gamma gone (gate flip; data retained — her credit_underwriting consent still
covers it). Then revoke Bank Gamma **credit_underwriting** → Scale Ops: watch the
erasure job (txns + snapshots deleted in batches), audit ticker shows
`event_received → gate_flip (≈20 ms) → physical_erasure`.
To reset Mei Ling afterwards:
```bash
cd backend && uv run python - <<'EOF'
import asyncio
from acme_ofv.db import make_async_client, ofv_db
async def m():
    db = ofv_db(make_async_client())
    await db.seed_link_plan.update_many(
        {"customer_id": "acme_cust_000003", "dp_id": "DP-BANKC-001-9C2B"},
        {"$set": {"status": "pending"}})
asyncio.run(m())
EOF
uv run python -m acme_ofv.seed.link_all
```

## 5. The storm
Scale Ops → pick 200/500 consents → **FIRE**. Watch: revocations drain, erasure
docs climb, read p50/p99 measured against live One View reads. (Numbers from a
laptop include WAN RTT to ap-southeast-1 — run from an EC2 in the peered VPC
for the headline single-digit figures.)

## 6. PFM analytics
Aisyah → Insights: spend donut (MoM deltas), Money In/Out, net-worth trend
(from `balance_snapshots` — the data the Open Finance platform doesn't serve), safe-to-spend,
recurring with zombie flags, money map. All aggregation pipelines, no export.

## 7. Underwriting
Aisyah → Underwriting → Run scorecard: decision in ~100 ms end-to-end, score
computed **inside an aggregation pipeline**, then "view snapshot" — verbatim
consent copies + the exact per-account feature components used. Open
`underwriting_runs` in Compass for the audit-defensibility story.

## 8. Spec fidelity flex
Open http://localhost:8100/docs next to the the Open Finance platform PDF: same objects, enums,
pagination, error codes, lifecycle. Consent renewal: link the same
(DP, purpose) again in the Consent Centre → predecessor auto-revoked with
`reason_code: duplicate` (visible in History).

## Chaos toggles (Q&A ammunition)

```bash
# 429 storm on Bank Beta (watch ingestion back off 5/10/20/40s × BACKOFF_SCALE)
curl -X POST localhost:8100/admin/chaos -d '{"rate_storm_dps": ["DP-BANKB-001-7F3A"]}'
# DP-side suspension → platform→DC webhook → gate flip, no DC code involved
curl -X POST localhost:8100/admin/dp-action/<consent_id>/suspend
# clear
curl -X POST localhost:8100/admin/chaos -d '{"rate_storm_dps": []}'
```

## Ports
mock OFP 8100 · DC API 8010 · frontend 5174 (8000/5173 are used by other demos
on this machine).
