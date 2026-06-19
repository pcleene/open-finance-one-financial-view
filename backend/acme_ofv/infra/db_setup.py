"""Create acme_ofv collections, validators and indexes (brief §4). Idempotent.

Run:  uv run python -m acme_ofv.infra.db_setup
"""

from pymongo import ASCENDING as A
from pymongo import DESCENDING as D
from pymongo.errors import CollectionInvalid

from acme_ofv.db import make_sync_client, ofv_db_sync

TXN_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "customer_id", "transaction_id", "transaction_date",
            "credit_debit_indicator", "amount", "is_settled", "account",
        ],
        "properties": {
            # Slimmed consent stamp (brief §1): provenance-only, external rows
            # only; internal rows omit it (absence = Acme-internal data).
            "consent_id": {"bsonType": ["string", "null"]},
            "credit_debit_indicator": {"enum": ["credit", "debit"]},
            "amount": {
                "bsonType": "object",
                "required": ["amount", "currency"],
                "properties": {
                    "amount": {"bsonType": "decimal"},
                    "currency": {"bsonType": "string", "maxLength": 3},
                },
            },
            "transaction_date": {"bsonType": "date"},
            "is_settled": {"bsonType": "bool"},
            "mcc": {"bsonType": ["string", "null"], "maxLength": 4},
        },
    }
}

CONSENT_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["consent_id", "dc_id", "dp_id", "consent_purpose", "status",
                     "permissions", "expiration_datetime"],
        "properties": {
            "consent_purpose": {"enum": ["pfm", "credit_underwriting"]},
            "status": {"enum": [
                "awaiting_authorization", "authorized", "failed", "rejected",
                "suspended", "expired", "revoked",
            ]},
            "expiration_datetime": {"bsonType": "date"},
        },
    }
}


def ensure_collection(db, name, **kwargs):
    try:
        db.create_collection(name, **kwargs)
        print(f"  created {name}")
    except CollectionInvalid:
        # Already exists — keep the validator CURRENT via collMod. create_collection
        # is a no-op on an existing collection, so without this a changed validator
        # (e.g. the slimmed consent stamp: required `consent` object → optional
        # `consent_id` string) silently never reaches the live collection, and fresh
        # inserts get rejected with "Document failed validation".
        validator = kwargs.get("validator")
        if validator is not None:
            db.command("collMod", name, validator=validator,
                       validationLevel=kwargs.get("validationLevel", "moderate"))
            print(f"  exists  {name} (validator synced via collMod)")
        else:
            print(f"  exists  {name}")


def main() -> None:
    client = make_sync_client()
    db = ofv_db_sync(client)
    print(f"setting up {db.name} on {client.address or 'cluster'}")

    ensure_collection(db, "customer_profiles")
    ensure_collection(db, "transactions", validator=TXN_VALIDATOR, validationLevel="moderate")
    ensure_collection(db, "consents", validator=CONSENT_VALIDATOR, validationLevel="moderate")
    ensure_collection(db, "institutions")
    ensure_collection(
        db, "balance_snapshots",
        timeseries={"timeField": "as_of", "metaField": "meta", "granularity": "hours"},
        expireAfterSeconds=60 * 60 * 24 * 396,  # 13-month underwriting lookback
    )
    ensure_collection(db, "uw_features")
    ensure_collection(db, "underwriting_runs")
    ensure_collection(db, "pfm_monthly_rollups")
    ensure_collection(db, "consent_audit_log")
    ensure_collection(db, "erasure_jobs")
    ensure_collection(db, "ofp_pull_ledger")
    ensure_collection(db, "stream_checkpoints")
    ensure_collection(db, "simulation_runs")

    print("indexes:")
    db.customer_profiles.create_index(
        [("customer.hashed_id_number", A)], unique=True, name="hashed_id_unique")
    db.customer_profiles.create_index([("accounts.account_id", A)], name="acct_lookup")
    db.customer_profiles.create_index(
        [("accounts.consents.consent_id", A)], name="consent_lookup")
    db.customer_profiles.create_index(
        [("accounts.consents.expiration_datetime", A)],
        partialFilterExpression={"accounts.consents.status": "authorized"},
        name="expiry_sweep")
    print("  customer_profiles: 4")

    db.transactions.create_index(
        [("customer_id", A), ("account.account_id", A), ("transaction_date", D)],
        name="cust_acct_date")
    db.transactions.create_index(
        [("customer_id", A), ("transaction_date", D)], name="cust_date")
    # provenance join-back consent_id -> consents (external rows only; sparse so
    # internal rows that omit the field cost nothing) — brief §1
    db.transactions.create_index(
        [("consent_id", A)], name="consent_provenance", sparse=True)
    print("  transactions: 3 (lean per §4.2 + sparse consent_id provenance)")

    db.consents.create_index(
        [("customer_id", A), ("status", A), ("consent_purpose", A)], name="gate_fallback")
    db.consents.create_index(
        [("hashed_id_number", A), ("dp_id", A), ("consent_purpose", A), ("status", A)],
        name="uniqueness_rule")
    db.consents.create_index(
        [("expiration_datetime", A)],
        partialFilterExpression={"status": "authorized"}, name="expiry_sweep")
    db.consents.create_index([("accounts.account_id", A)], name="acct_lookup")
    print("  consents: 4")

    db.underwriting_runs.create_index([("customer_id", A), ("run_at", D)], name="cust_runs")
    db.consent_audit_log.create_index([("consent_id", A), ("at", D)], name="consent_audit")
    db.consent_audit_log.create_index([("at", D)], name="recent_events")
    db.erasure_jobs.create_index([("status", A), ("created_at", D)], name="job_status")
    db.ofp_pull_ledger.create_index([("consent_id", A), ("at", D)], name="pull_by_consent")
    db.pfm_monthly_rollups.create_index(
        [("customer_id", A), ("month", A), ("category", A)], unique=True, name="rollup_key")
    db.simulation_runs.create_index([("started_at", D)], name="recent_runs")
    print("  support collections: done")

    client.close()
    print("db_setup complete")


if __name__ == "__main__":
    main()
