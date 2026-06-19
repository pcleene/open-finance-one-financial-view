"""Seed the POC dataset.

Writes:
  ofp_mock   — providers, users, accounts, transactions  (the DP side)
  acme_ofv   — institutions mirror, customer_profiles (internal accounts
               embedded), internal transactions (source=acme_core),
               90 d balance_snapshots for internal accounts, seed_link_plan

External-bank data lands in acme_ofv ONLY via consent + ingestion (run
link_all.py with the mock + DC API up) — the pipeline being demonstrated.

Run:  uv run python -m acme_ofv.seed.run_seed --cohort 200 [--procs 4]
"""

import argparse
import random
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool

from pymongo import ASCENDING as A, DESCENDING as D

from acme_ofv.db import make_sync_client, mock_db_sync, ofv_db_sync
from acme_ofv.ingestion.enrichment import categorize
from acme_ofv.seed.gen import (
    Acme_INTERNAL,
    PROVIDERS,
    Customer,
    d128,
    gen_transactions,
    plan_external_accounts,
    plan_internal_accounts,
)

PERSONAS = [
    {
        "idx": 1,
        "persona": {"full_name": "Aisyah binti Rahman", "female": True, "salary": 7400.0,
                    "payday": 25, "employer": "TechVista Sdn Bhd", "segment": "preferred",
                    "gambler": False, "email": "a****h@gmail.com"},
        "external_dps": ["DP-BANKB-001-7F3A", "DP-BANKC-001-9C2B", "DP-NPF-001-2F9E"],
        "consents": [
            ("DP-BANKB-001-7F3A", "pfm", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-BANKB-001-7F3A", "credit_underwriting", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-BANKC-001-9C2B", "pfm", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-BANKC-001-9C2B", "credit_underwriting", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-NPF-001-2F9E", "pfm", ["read_accounts", "read_balances"]),
        ],
    },
    {
        "idx": 2,
        "persona": {"full_name": "Farid bin Osman", "female": False, "salary": 5200.0,
                    "payday": 27, "employer": "Sunway Group", "segment": "mass",
                    "gambler": False, "email": "f****d@gmail.com"},
        "external_dps": ["DP-BANKE-001-8A5C", "DP-NPF-001-2F9E"],
        "consents": [  # pfm-only → underwriting console must 403
            ("DP-BANKE-001-8A5C", "pfm", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-NPF-001-2F9E", "pfm", ["read_accounts", "read_balances"]),
        ],
    },
    {
        "idx": 3,
        "persona": {"full_name": "Mei Ling Tan", "female": True, "salary": 9800.0,
                    "payday": 26, "employer": "Axiata", "segment": "preferred",
                    "gambler": False, "email": "m****g@gmail.com"},
        "external_dps": ["DP-BANKC-001-9C2B", "DP-BANKD-001-4E1D"],
        "consents": [  # revocation showcase
            ("DP-BANKC-001-9C2B", "pfm", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-BANKC-001-9C2B", "credit_underwriting", ["read_accounts", "read_balances", "read_transactions"]),
            ("DP-BANKD-001-4E1D", "pfm", ["read_accounts", "read_balances", "read_transactions"]),
        ],
    },
]

BANK_DPS = [p["provider_id"] for p in PROVIDERS if p["provider_type"] == "bank"]
NPF = "DP-NPF-001-2F9E"


def build_customer(idx: int, rng: random.Random, persona_cfg: dict | None = None):
    """Generate one customer end-to-end. Returns docs for both databases."""
    if persona_cfg:
        c = Customer(idx, rng, persona_cfg["persona"])
        external_dps = persona_cfg["external_dps"]
        consents = persona_cfg["consents"]
    else:
        c = Customer(idx, rng)
        n_banks = rng.choices([1, 2, 3], weights=[5, 4, 1])[0]
        external_dps = rng.sample(BANK_DPS, k=n_banks)
        if rng.random() < 0.45:
            external_dps.append(NPF)
        consents = []
        for dp in external_dps:
            if dp == NPF:
                consents.append((dp, "pfm", ["read_accounts", "read_balances"]))
            else:
                consents.append((dp, "pfm", ["read_accounts", "read_balances", "read_transactions"]))
                if rng.random() < 0.4:
                    consents.append((dp, "credit_underwriting",
                                     ["read_accounts", "read_balances", "read_transactions"]))

    plan_internal_accounts(c)
    plan_external_accounts(c, external_dps)

    external = [a for a in c.accounts if a["dp_id"] != Acme_INTERNAL]
    internal = [a for a in c.accounts if a["dp_id"] == Acme_INTERNAL]

    mock_txns = [t for a in external for t in gen_transactions(c, a)]
    internal_txns_raw = [(a, gen_transactions(c, a)) for a in internal]

    now = datetime.now(timezone.utc)

    # ---- DC-side internal transaction docs (source=acme_core) ----
    internal_txn_docs = []
    for a, txns in internal_txns_raw:
        for t in txns:
            doc = {k: v for k, v in t.items() if k != "dp_id"}
            doc["_id"] = f"{Acme_INTERNAL}::{t['transaction_id']}"
            doc["schema_version"] = 2
            doc["customer_id"] = c.customer_id
            doc["hashed_id_number"] = c.hashed_id_number
            doc["account"] = {
                "account_id": a["account_id"], "dp_id": Acme_INTERNAL,
                "institution_name": "Acme Bank Berhad", "type": a["type"],
                "subtype": a["subtype"],
                "account_number_masked": a["account_number_display"],
                "is_internal": True,
            }
            # Internal Acme rows are not OFP-governed: omit the consent field
            # entirely (brief §1). Absence is the signal; account.is_internal:true
            # already says it — no null-padding cost in the document model.
            doc["enrichment"] = categorize(t)
            if "OWN ACCOUNT" in t["description"]:
                doc["enrichment"]["is_transfer_own_account"] = True
            doc["ingest"] = {"ingested_at": now, "x_fapi_interaction_id": None,
                             "source": "acme_core"}
            internal_txn_docs.append(doc)

    # ---- profile with embedded internal accounts ----
    embedded = []
    net = 0.0
    for a in internal:
        bal = a["balance_state"]
        cur = float(bal["current_balance"]["amount"].to_decimal())
        net += cur if bal["current_balance"]["credit_debit_indicator"] == "credit" else -cur
        embedded.append({
            "account_id": a["account_id"],
            "account_number": a["account_number"],
            "account_number_masked": a["account_number_display"],
            "account_name": a["account_name"],
            "account_holder_name": a["account_holder_name"],
            "institution_name": "Acme Bank Berhad",
            "category": a["category"], "type": a["type"], "subtype": a["subtype"],
            "limit": a["limit"], "interest_rate": a["interest_rate"],
            "minimum_payment_amount": a["minimum_payment_amount"],
            "payment_due_date": a["payment_due_date"],
            "loan_details": a["loan_details"],
            "custom_data": None,
            "dp_id": Acme_INTERNAL,
            "is_internal": True,
            "balances": {**bal, "as_of": now},
            "consents": [],
            "sync": {"last_full_sync_at": now, "last_txn_date_pulled": None,
                     "last_txn_cursor": None, "consecutive_failures": 0, "last_error": None},
        })

    # capped recent_transactions embedded for One View's single read (brief §3);
    # refresh_summary rebuilds this (incl. external rows) after each backfill.
    recent_seed = [{
        "transaction_id": d["transaction_id"],
        "transaction_date": d["transaction_date"],
        "amount": d["amount"],
        "credit_debit_indicator": d["credit_debit_indicator"],
        "description": d["description"],
        "is_settled": d["is_settled"],
        "account": {"account_id": d["account"]["account_id"],
                    "dp_id": d["account"]["dp_id"],
                    "institution_name": d["account"]["institution_name"]},
        "enrichment": {"merchant_normalized": d["enrichment"].get("merchant_normalized"),
                       "category": d["enrichment"].get("category")},
    } for d in sorted(internal_txn_docs, key=lambda x: x["transaction_date"],
                      reverse=True)[:15]]

    profile = {
        "_id": c.customer_id,
        "schema_version": 2,
        "customer": {
            "cif_number": f"CIF{rng.randint(10**7, 10**8 - 1)}",
            "full_name": c.full_name,
            "preferred_name": c.full_name.split()[0],
            "id_type": "nric",
            "hashed_id_number": c.hashed_id_number,
            "segment": c.segment,
            "octo_user_id": f"octo_{rng.randint(10**6, 10**7 - 1)}",
            "ofv_activated_at": now,
            "email_masked": c.email,
            "risk_flags": [],
        },
        "accounts": embedded,
        "recent_transactions": recent_seed,
        "summary": {
            "net_position": {"amount": d128(round(net, 2)), "currency": "MYR"},
            "institutions_linked": ["Acme"],
            "external_account_count": 0,
            "active_consent_count": 0,
            "last_refreshed_at": now,
        },
        "pfm_settings": {
            "budgets": [
                {"category": "food_and_beverage",
                 "monthly_limit": d128(float(rng.choice([600, 800, 1000]))),
                 "alert_threshold": 0.8},
                {"category": "shopping",
                 "monthly_limit": d128(float(rng.choice([400, 600, 900]))),
                 "alert_threshold": 0.8},
            ],
            "goals": [],
            "payday_day_of_month": None,  # detected by PFM later
        },
        "audit": {"created_at": now, "updated_at": now},
    }

    # ---- 90 d balance snapshots for internal accounts (random walk back) ----
    snapshots = []
    for a in internal:
        cur = float(a["balance_state"]["current_balance"]["amount"].to_decimal())
        indicator = a["balance_state"]["current_balance"]["credit_debit_indicator"]
        v = cur
        for back in range(90):
            day = now - timedelta(days=back)
            snapshots.append({
                "as_of": day.replace(hour=23, minute=59, second=0, microsecond=0),
                "meta": {"customer_id": c.customer_id, "account_id": a["account_id"],
                         "dp_id": Acme_INTERNAL, "type": a["type"]},
                "current_balance": d128(round(max(v, 0.0), 2)),
                "available_balance": d128(round(max(v, 0.0), 2)),
                "credit_debit_indicator": indicator,
                "currency": "MYR",
            })
            v *= rng.uniform(0.96, 1.045)

    link_plan = [
        {"customer_id": c.customer_id, "dp_id": dp, "consent_purpose": purpose,
         "permissions": perms, "validity_days": 180, "status": "pending"}
        for dp, purpose, perms in consents
    ]

    mock_accounts = [dict(a) for a in external]
    return {
        "user": c.user_doc(),
        "mock_accounts": mock_accounts,
        "mock_txns": mock_txns,
        "profile": profile,
        "internal_txns": internal_txn_docs,
        "snapshots": snapshots,
        "link_plan": link_plan,
    }


def insert_batch(results: list[dict], mdb, odb) -> None:
    def push(coll, docs):
        if docs:
            coll.insert_many(docs, ordered=False)
    push(mdb.users, [r["user"] for r in results])
    push(mdb.accounts, [a for r in results for a in r["mock_accounts"]])
    push(mdb.transactions, [t for r in results for t in r["mock_txns"]])
    push(odb.customer_profiles, [r["profile"] for r in results])
    push(odb.transactions, [t for r in results for t in r["internal_txns"]])
    push(odb.balance_snapshots, [s for r in results for s in r["snapshots"]])
    push(odb.seed_link_plan, [lp for r in results for lp in r["link_plan"]])


def seed_slice(args) -> int:
    """Worker: generate + insert customers [start, start+count). Own client + PRNG."""
    start, count, seed = args
    client = make_sync_client()
    mdb, odb = mock_db_sync(client), ofv_db_sync(client)
    rng = random.Random(seed)
    batch, n = [], 0
    for i in range(start, start + count):
        batch.append(build_customer(i, rng))
        if len(batch) >= 50:
            insert_batch(batch, mdb, odb)
            n += len(batch)
            batch = []
    if batch:
        insert_batch(batch, mdb, odb)
        n += len(batch)
    client.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", type=int, default=200, help="synthetic customers beyond the 3 personas")
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="do not wipe existing data")
    args = ap.parse_args()

    client = make_sync_client()
    mdb, odb = mock_db_sync(client), ofv_db_sync(client)

    if not args.keep:
        print("wiping previous data …")
        for coll in ("users", "accounts", "transactions", "consents", "tokens",
                     "par_requests", "auth_codes", "providers"):
            mdb[coll].delete_many({})
        for coll in ("customer_profiles", "transactions", "consents", "balance_snapshots",
                     "uw_features", "underwriting_runs", "pfm_monthly_rollups",
                     "consent_audit_log", "erasure_jobs", "ofp_pull_ledger",
                     "seed_link_plan", "institutions"):
            odb[coll].delete_many({})

    print("providers + institutions mirror …")
    now = datetime.now(timezone.utc)
    mdb.providers.insert_many([
        {**{k: v for k, v in p.items() if k != "custom_dialect"},
         "authorization_server_url": "http://127.0.0.1:8100",
         "resource_server_url": "http://127.0.0.1:8100"}
        for p in PROVIDERS
    ])
    odb.institutions.insert_many([
        {"_id": p["provider_id"], "name": p["name"], "status": p["status"],
         "provider_type": p["provider_type"],
         "authorization_server_url": "http://127.0.0.1:8100",
         "resource_server_url": "http://127.0.0.1:8100",
         "supported_use_cases": p["supported_use_cases"],
         "display": p["display"], "synced_at": now}
        for p in PROVIDERS
    ] + [{"_id": Acme_INTERNAL, "name": "Acme Bank Berhad", "status": "online",
          "provider_type": "bank", "supported_use_cases": [],
          "display": {"short_name": "Acme", "brand_color": "#EC0000", "logo_key": "acme"},
          "synced_at": now}])

    print("mock-side indexes …")
    mdb.users.create_index([("hashed_id_number", A)], unique=True)
    mdb.accounts.create_index([("account_id", A)], unique=True)
    mdb.accounts.create_index([("dp_id", A), ("hashed_id_number", A)])
    mdb.transactions.create_index(
        [("account_id", A), ("transaction_date", D), ("transaction_id", D)])
    mdb.consents.create_index([("consent_id", A)], unique=True)
    mdb.consents.create_index(
        [("hashed_id_number", A), ("dp_id", A), ("dc_id", A), ("consent_purpose", A), ("status", A)])
    mdb.tokens.create_index([("token", A)], unique=True)
    mdb.par_requests.create_index([("request_uri", A)])
    mdb.auth_codes.create_index([("code", A)])

    print("personas …")
    rng = random.Random(42)
    insert_batch([build_customer(p["idx"], rng, p) for p in PERSONAS], mdb, odb)

    if args.cohort:
        print(f"cohort of {args.cohort} (procs={args.procs}) …")
        base = 1000
        if args.procs <= 1:
            seed_slice((base, args.cohort, 1042))
        else:
            per = -(-args.cohort // args.procs)
            slices = [(base + i * per, min(per, args.cohort - i * per), 1042 + i)
                      for i in range(args.procs) if args.cohort - i * per > 0]
            with Pool(args.procs) as pool:
                pool.map(seed_slice, slices)

    print("seed summary:")
    for name, coll in [("mock users", mdb.users), ("mock accounts", mdb.accounts),
                       ("mock transactions", mdb.transactions),
                       ("profiles", odb.customer_profiles),
                       ("internal txns", odb.transactions),
                       ("snapshots", odb.balance_snapshots),
                       ("link plan", odb.seed_link_plan)]:
        print(f"  {name:18} {coll.estimated_document_count():>10,}")
    client.close()


if __name__ == "__main__":
    main()
