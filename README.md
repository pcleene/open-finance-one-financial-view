# Acme One Financial View — POC

MongoDB Atlas as the consent-governed aggregation and serving layer for Acme's
consumer-side Open Finance, ingesting external-bank data via a spec-faithful
**Open Finance Data Consumer API** mock, with consent enforced at query
time across One View, PFM, and credit underwriting.

> **Sanitized public reference.** "Acme Bank" and the provider banks
> (Bank Beta/Gamma/Delta/Epsilon, NPF) are fictional placeholders; all data is
> **synthetic** and all infrastructure identifiers (AWS account, ARNs, DNS, IPs,
> cluster hosts) are placeholders. Provide your own MongoDB Atlas cluster and
> credentials via `.env` (see `.env.example`). No secrets are committed.

## Topology

| Process | Port | What it is |
|---|---|---|
| `mock_ofp` | 8100 | Open Finance Platform mock — AS (PAR/authorize/token/introspect) + RS (providers, consents+LCM, accounts, balances, transactions) + chaos toggles. Store: `ofp_mock` db |
| `api` | 8010 | DC service layer — One View (Path A single read), PFM, underwriting, consent centre + link flow, webhook receiver, scale-ops (SSE + storm) |
| `worker` | — | Eraser / profile-updater / feature-updater — consents change stream → gate-flip ACID txn → chunked erasure; backfill executor; expiry sweeper |
| `frontend` | 5174 | SvelteKit app (6 pages) |

Database: **`acme_ofv`** on Atlas `your-cluster` (X.509 auth, cert in `secrets/`).
Collections per brief §4: `customer_profiles` (consolidated, consent boxes
embedded), `transactions` (consolidated, consent ctx stamped per row),
`consents` (spec mirror), `institutions`, `balance_snapshots` (timeseries),
`uw_features`, `underwriting_runs`, `pfm_monthly_rollups`,
`consent_audit_log`, `erasure_jobs`, `ofp_pull_ledger`, `stream_checkpoints`.

## Run everything

```bash
cd backend

# one-time
uv sync
uv run python -m acme_ofv.infra.db_setup            # collections + validators + indexes
uv run python -m acme_ofv.seed.run_seed --cohort 200 --procs 4

# services (3 terminals, or ops/start_all.sh)
uv run uvicorn acme_ofv.mock_ofp.app:app --port 8100
uv run uvicorn acme_ofv.api.app:app --port 8010
uv run python -m acme_ofv.eraser.worker

# link every planned consent through the real PAR→authorize→token flow
uv run python -m acme_ofv.seed.link_all

# frontend
cd ../frontend && npm i && npm run dev               # http://localhost:5174
```

Full reseed: `ops/reseed.sh`.

> **Run order matters for the data split.** `run_seed.py` writes external-bank
> data into the **`ofp_mock`** database and only Acme-internal rows
> (`ingest.source: acme_core`) into `acme_ofv.transactions`. External-bank rows
> (`ingest.source: ofp_pull`) land in `acme_ofv` **only after** `link_all.py`
> runs with `mock_ofp` + `api` + the `worker` all up — that is the consent +
> ingestion pipeline being demonstrated. If `acme_ofv.transactions` looks
> Acme-only, the ingestion step hasn't completed yet. `link_all.py` now prints a
> per-`source`/`dp_id` breakdown at the end so the split is visible after each run.

## Demo personas

| Persona | id | Story |
|---|---|---|
| **Aisyah binti Rahman** | `acme_cust_000001` | Full stack: Acme + Bank Beta + Bank Gamma + NPF, both purposes — One View, PFM, underwriting run |
| **Farid bin Osman** | `acme_cust_000002` | pfm-only — rich insights, underwriting **403 Consent.InvalidScope** |
| **Mei Ling Tan** | `acme_cust_000003` | Revocation showcase — revoke Bank Gamma live, watch the gate flip + erasure |

Plus a 200-customer cohort behind the revocation storm (`/ops`).

## The consent event path (what's being demonstrated)

```
Consent Centre click / webhook / link flow
  → full post-image event (monotonic _rcp_version)
  → [transport: direct = sink-equivalent guarded upsert | kafka = MSK + Kafka Connect]
  → acme_ofv.consents
  → change stream (worker)
  → gate-flip ACID txn (profile consent boxes + audit log)   ← reads exclude data from this commit
  → physical erasure (chunked 2 000-_id batches, set-difference of still-covered accounts)
```

`CONSENT_EVENT_TRANSPORT=direct` (default) applies exactly the write the
MongoDB Kafka sink connector would (ReplaceOneBusinessKey upsert, version
guard). The production MSK + Kafka Connect path is config-only:
`ops/kafka/` holds the connector JSON per brief §7; everything downstream of
the topic is identical.

## Engineering standards honoured (brief §15)

- PyMongo Async (`AsyncMongoClient`) everywhere — no Motor.
- No `$regex` in any DB query; categorization is write-time enrichment.
- Amounts `Decimal128` end-to-end; spec string amounts parsed at the boundary.
- UTC storage, MYT rendering, EOD-expiry (`23:59:59+08:00`) comparisons.
- Multi-doc transactions only for the gate flip + audit invariant.
- Change streams resume from tokens persisted in `stream_checkpoints`.
- Idempotency: `_id = dp_id::transaction_id`, upserts, `_rcp_version` guards.
