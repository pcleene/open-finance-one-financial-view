# Acme Open Finance — "One Financial View" · Solution Walkthrough

> 🎤 **Speaker note (open with this):** "Everything you're about to see runs on one MongoDB Atlas cluster, against data we pulled the way the Open Finance spec says to pull it. There's no warehouse, no nightly batch, no copy of the data for analytics. The same document the customer sees in their app is the document the banker underwrites on — and the document the privacy team erases on revocation. One model, three audiences."

---

## How to use this document

This is two documents in one:

- **The narrative** (normal text + *The story* / *On screen* sections) is client-ready — read it top to bottom and it explains the solution.
- **The speaker notes** (📣 call-outs) are your talk track — the punchline to land at each step.
- **The "Under the hood" blocks** show the *actual* API endpoint and the *actual* MongoDB query from the codebase, with a plain-English explanation of **why it's built that way and where the MongoDB value is**. Expand them live when an engineer in the room wants to go deeper.
- Every block is tagged with its **exact file path + line range** so you can jump straight into the app and show the real code while presenting. Paths are relative to the repo root; all backend code lives under `backend/acme_ofv/`. The **Code source map** in the appendix lists every route → service → pipeline location in one place. *(Line numbers are accurate as of this revision; if the code shifts, search the function name shown next to each path.)*
- Each section opens with two tags: **📋 Open Finance DC API** — which requirement sections of the spec it addresses — and **🍃 MongoDB capability** — how the document model, aggregation framework, change streams and (especially) flexible schema deliver it. A consolidated **Open Finance requirement coverage** map is in the appendix.

The spine of the whole platform is three moves:

1. **Fetch** consented data from the Open Finance Portal (OFP), the way a licensed Data Consumer must.
2. **Consolidate** it into a customer-centric document model in Atlas — enriched once, at write time.
3. **Serve** every experience (customer app, PFM insights, credit underwriting) from that one model, with **consent enforced inside the read itself**.

| Demo surface | Who it's for | What it proves |
|---|---|---|
| **One View** | Customer | The whole cross-bank position in *one* indexed read |
| **Transactions + Search** | Customer | Hybrid (text + vector) search, consent-scoped |
| **Insights** | Customer | Spend / cashflow / net-worth computed *in the database* |
| **Underwriting** | Acme banker | A scorecard computed *inside* an aggregation, on consented data only |
| **Scale Ops** | Acme platform | Revoke-at-scale + physical erasure while reads stay flat |

> 🎤 **Framing line:** "The hard part of Open Finance isn't reading an API. It's what you do with the data *after* — keeping it consolidated, queryable, governed, and auditable at population scale. That's the MongoDB story."

---

# a. Fetching data from the Open Finance portal

> 📋 **Open Finance DC API — addressed:** §3.1 DC Integration Flow (auth-code grant) + §3.4 Resource Server Flow · §4.3 PAR / Account Access Consent Request · §4.5 Token (client-credentials grant) · §5.5 Account (+ Loan Details) · §5.6 Account Balances · §5.7 Account Transactions · **§8 Custom Data** · §9 Pagination (+ §10 Meta Object) · §12 FAPI Headers · §11 Security (mTLS, JWE-in-JWS).
>
> 🍃 **MongoDB capability:** the document model + idempotent `_id` upserts, `Decimal128` money, and write-time enrichment — and a **flexible / polymorphic schema** that absorbs each institution's differently-shaped objects (see *MongoDB's flexible schema* at the end of this section).

### The story

Acme is a licensed **Data Consumer (DC)**. When a customer authorises Acme to see their accounts at another bank, the platform's Open Finance Portal (OFP) issues a **consent** scoped to specific accounts, permissions (`read_accounts`, `read_balances`, `read_transactions`), and an expiry. Acme then pulls that customer's account objects, balances and transaction history — politely (rate-limited), resumably (cursor pagination), and idempotently (so a retry never doubles a row).

We do **one more thing** at the moment of ingest that defines the rest of the demo: we **enrich and consolidate** each transaction into a single customer-centric document model, instead of dropping raw rows into a lake. Categorisation, merchant normalisation, the owning account, and the consent provenance are all stamped *once*, at write time. Every downstream read is then cheap.

### On screen

- **Scale Ops → "Consolidated rows"** shows the live consolidated count.
- **Manage Profile** (top-right) → link a new institution → watch the **Live pipeline feed** light up `authorized → backfill`.
- The **Simulation** screen drives this same path at volume (spec-faithful: rate limits, 429 back-off, cursor paging).

### Under the hood

A newly **authorized** consent event flows through one ordered path and lands on the eraser/back-fill worker, which calls `backfill_consent()`. For each account in scope it pulls the **account object**, the **balance** (point-in-time), then **paginates transactions**.

**OFP / mock endpoints used (Open Finance DC API):**

| Call | Purpose |
|---|---|
| `POST /v1/oauth/token` | Client-credentials token for the DC |
| `GET /v1/accounts/{account_id}` | Account object (masked number, type, limits) |
| `GET /v1/accounts/{account_id}/balances` | Point-in-time balance → the snapshot anchor |
| `GET /v1/accounts/{account_id}/transactions` | Cursor-paginated transaction history |

**The transaction pull — enrich + idempotent upsert, page by page**
📄 **`backend/acme_ofv/ingestion/backfill.py:170–240`** · `pull_transactions()` — the loop is lines **195–240**; the full per-consent backfill is `backfill_consent()`, lines **26–158** (balance anchor + reconstruction at **90–138**).

```python
while True:
    p = dict(params)
    if cursor:
        p["next_page_params"] = cursor          # opaque cursor — replayed verbatim
    r = await client.get(f"/v1/accounts/{account_id}/transactions", params=p)
    rows = r.json().get("data", [])

    ops = []
    for t in rows:
        doc = {
            "_id": f"{dp_id}::{t['transaction_id']}",   # idempotency key → retry-safe
            "schema_version": 2,
            "customer_id": customer_id,
            **t,
            "transaction_date": parse_dt(t["transaction_date"]),
            "amount": amount_to_decimal(t["amount"]),   # Decimal128 — exact money
            "account": account_ctx,                      # embedded owning-account context
            "consent_id": consent_id,                    # slim provenance stamp (see below)
            "enrichment": categorize(t),                 # category / merchant / recurring — at write time
            "ingest": {"ingested_at": now, "source": "ofp_pull"},
        }
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True))
    await db.transactions.bulk_write(ops, ordered=False)        # bulk, unordered

    cursor = (body.get("meta") or {}).get("next_page_params")
    await db.customer_profiles.update_one(                       # persist cursor → resumable
        {"_id": customer_id, "accounts.account_id": account_id},
        {"$set": {"accounts.$.sync.last_txn_cursor": cursor}})
    if not cursor:
        break
```

**Why it's built this way — the MongoDB value:**

- **`_id = dp_id::transaction_id`** makes every write idempotent. A crash mid-pull, a 429 retry, or a re-run of the same page is a no-op `$set` upsert — never a duplicate. This is how you get *exactly-once* ingestion semantics out of an *at-least-once* API.
- **Enrich at write time, read many times.** `categorize(t)` computes category, normalised merchant and recurring signals *once*. The 400M-row read path never pays for it again — no ETL job, no second system.
- **Embed the owning-account context** (`account`) on the row. Insights and search filter on `account.account_id` without a join.
- **Slim consent stamp.** External rows carry a single `consent_id` string — pure provenance ("under which lawful basis was this row collected"). The live consent state lives in the profile (next section), so we never stamp mutable status on 400M rows. *Net saving at full scale: ~20–25 GB.*
- **Decimal128 for money** (`amount_to_decimal`) — no float drift, ever.

After the rows land, the balance **anchor** is inserted into the `balance_snapshots` time-series collection and the 90-day EOD series is **reconstructed by walking the transactions backward** from that real balance — then the profile summary and recurring detector run. Notably, **`uw_features` is *not* built here** — it's built on demand at loan inquiry (see *Underwriting*).

> 🎤 **Speaker note:** "Two things to land here. One — every write is idempotent, so retries and back-off are free; that's the `_id` trick. Two — we enrich *once*, at ingest. Competitors land raw rows and run a nightly Spark job to categorise. We do it inline and never touch it again. That's why the read path is single-digit milliseconds at the end of this demo."

> 💡 **Objection — "Isn't enriching at write expensive?"** It's a per-row function over data we're already writing; it adds microseconds and removes an entire downstream pipeline. The expensive thing is re-deriving category over billions of rows at read time — which is exactly what we avoid.

### MongoDB's flexible schema — the polymorphic data problem the spec hands you

> 🎤 **Speaker note (the killer MongoDB point):** "the platform's *own* spec guarantees the data is **not** uniform. Every Account, Balance and Transaction object can carry an optional `custom_data` bag (§8), and half the fields are **conditional** — a loan has `loan_details`, a card has `mcc` and a credit limit, a deposit has `transfer_method`. So Bank Beta, Acme and NPF literally return different-shaped objects. In a relational world that's either a 200-column sparse table full of NULLs or a table-per-type join maze — and onboarding a new bank is a schema migration. In MongoDB it's just… documents: we store each shape as-is, index what we need, and a new institution (or a new `custom_data` key) needs **zero DDL**."

**What the the Open Finance platform spec itself guarantees is heterogeneous — so it can't be one fixed table:**

| Spec field | Spec | Present only when… |
|---|---|---|
| `custom_data` (≤ 20 k/v pairs) | §8 Custom Data | Optional on **every** Account / Balance / Transaction — DP-specific extras |
| `loan_details` (amount, origination, maturity) | §5.5 | `type = loan` |
| `limit`, `interest_rate`, `minimum_payment_amount`, `payment_due_date` | §5.5 | `type = credit` / `loan` |
| `transfer_method`, `transfer_submethod` | §5.7 | account `type = deposit` |
| `merchant_name`, `mcc` | §5.7 | `type = credit_card` / `deposit` |
| `foreign_currency_amount` | §5.7 | foreign-currency transactions |
| `recipient_reference`, `other_payment_description` | §5.7 | optional catch-alls each bank fills differently |

**How the document model absorbs it** — we keep the DP's raw fields *verbatim* (`**t`) and add our derived data in its own sub-document; the embedded account keeps `loan_details` / `custom_data` **only when the DP sent them**:

📄 `backend/acme_ofv/ingestion/backfill.py:211–228` (transaction) · `63–88` (embedded account)

```python
doc = {
    "_id": f"{dp_id}::{t['transaction_id']}",
    "customer_id": customer_id,
    **t,                            # ← every DP field kept verbatim: custom_data, mcc,
                                    #    recipient_reference, foreign_currency_amount, transfer_method…
    "amount": amount_to_decimal(t["amount"]),
    "account": account_ctx,
    "enrichment": categorize(t),    # our derived fields live in their OWN sub-document
}
# embedded account — loan_details / custom_data exist ONLY if the DP returned them:
embedded = {
    "account_id": account_id, "type": acc_obj.get("type"),
    "loan_details": ({**acc_obj["loan_details"], ...} if acc_obj.get("loan_details") else None),
    "custom_data":  acc_obj.get("custom_data"),
    # ... (is_internal accounts simply OMIT the consent block — absence is the signal)
}
```

**Why this is a MongoDB superpower here:**

- **One collection, many shapes.** `transactions` holds card, deposit, loan and NPF rows side by side; the `accounts` array on a profile holds every account type — no sparse columns, no per-type tables, no `UNION` views.
- **Onboarding a new institution = zero migration.** A DP that returns extra `custom_data` keys just… writes. No `ALTER TABLE`, no backfill job, no coordinated deploy, no downtime.
- **Flexible *and* governed.** We still pin the fields the gate and scorecard depend on (`schema_version`, validation, indexes) while leaving the long tail open — flexibility where the data varies, rigor where correctness matters.
- **The enrichment is additive.** Because derived signals live under `enrichment.*`, they never collide with whatever bespoke fields a bank ships in `custom_data` — the schema grows by *addition*, never by migration.

---

# b. The One View reads

> 📋 **Open Finance DC API — addressed:** §5.2 Retrieve Consent (Account Access Consent Object + Consented Accounts Object) · §13 Consent Framework · §15 Enums (Consent Status / Permission / Purpose). The DC must honour consent **status, permission, purpose and expiry** before exposing any data.
>
> 🍃 **MongoDB capability:** **embedded sub-documents** — the consent "boxes" live on each account inside the profile — so a single-document read with an in-document `$filter` enforces consent *inside* the query. No joins, no second lookup, no separate policy store.

### The story

This is the headline. A customer opens the app and sees **every account, every institution, their net position, and recent activity** — in one screen. Behind it is **one document read** of the customer's profile. No fan-out to N banks, no join across collections, and — critically — **consent is enforced inside that same read**. If a customer revoked Bank Beta yesterday, those accounts simply aren't in the result. Not hidden by app logic. *Not returned by the database.*

### On screen

- The landing page after picking a customer: net position, the account cards grouped by institution, recent transactions.
- Switch users (top-right → *demo only · switch user*) to show a customer with revoked consents — those institutions disappear from the view.

### Under the hood

```text
GET /one-view/{customer_id}      →  OneViewService.one_view()

route:    backend/acme_ofv/api/v1/routes/one_view.py:16–21
service:  backend/acme_ofv/services/one_view_service.py:61–88
pipeline: backend/acme_ofv/consent/gate.py:26–62  (one_view_pipeline)
```

The whole thing is **one aggregation** against `customer_profiles`. The consent filter is a `$filter` over the **embedded consent boxes** on each account — no pre-query lookup, no `$lookup`:

```python
# backend/acme_ofv/consent/gate.py:26–62 · one_view_pipeline()  — Path A: enforce consent INSIDE the read
# (the consent $filter "box_clause" is lines 32–39)
box_clause = {"$gt": [{"$size": {"$filter": {
    "input": {"$ifNull": ["$$acc.consents", []]}, "as": "c",
    "cond": {"$and": [
        {"$eq":  ["$$c.status", "authorized"]},                 # live status
        {"$in":  ["$$c.consent_purpose", ofp_purposes]},     # right purpose
        {"$in":  [permission, "$$c.permissions"]},              # right permission
        {"$gt":  ["$$c.expiration_datetime", now]},             # not expired (EOD self-enforcing)
    ]}}}}, 0]}

pipeline = [
    {"$match": {"_id": customer_id}},                           # 1 doc, by _id
    {"$project": {
        "customer": 1, "summary": 1, "pfm_settings": 1,
        "accounts": {"$filter": {                               # keep only consented accounts
            "input": "$accounts", "as": "acc",
            "cond": {"$or": [{"$eq": ["$$acc.is_internal", True]}, box_clause]},
        }},
        "recent_transactions": {"$ifNull": ["$recent_transactions", []]},
    }},
    {"$addFields": {                                            # recent activity, also consent-filtered
        "recent_transactions": {"$filter": {
            "input": "$recent_transactions", "as": "t",
            "cond": {"$in": ["$$t.account.account_id", "$accounts.account_id"]},
        }},
    }},
]
```

The service runs it and reports that it was a **single read**. The home screen's
budget alerts **ride this same profile read** — we derive the pfm scope from the
aggregate result and run one `transactions` aggregate for the spend, so there's no
second `customer_profiles` lookup:

```python
# backend/acme_ofv/services/one_view_service.py:61–88 · OneViewService.one_view()
rows = await aggregate_list(db.customer_profiles, one_view_pipeline(customer_id, "one_view"))
profile = rows[0]
recent = profile.pop("recent_transactions", [])
# budget alerts reuse THIS read: scope resolved from the same profile, one txn aggregate
budgets = await compute_budgets(db, customer_id, scope_from_profile(profile, "pfm"),
                                profile.get("pfm_settings", {}).get("budgets", []))
return {
    "profile": jsonable(profile),
    "recent_activity": jsonable(recent),
    "budgets": budgets,
    "latency_ms": {"profile_read": round(t_profile, 2)},
    "reads": {"profile": 1},                                    # ← one document, one index hit
}
```

**Why it's built this way — the MongoDB value:**

- **One document = one customer.** Because the data model is customer-centric (accounts embedded, recent transactions embedded), the entire home screen is a `find by _id`. In a relational world this is 5–10 joins across accounts, balances, institutions and a transaction table; here it's one index seek.
- **Consent enforcement is a property of the query, not the app.** The `$filter` on the embedded boxes means a revoked/expired/suspended consent is *invisible to the database read* on the very next call. There is no code path that can "forget" to apply consent, because the data and the gate live in the same place.
- **EOD expiry self-enforces.** `expiration_datetime > now` inside the pipeline means an expired consent drops out at midnight with zero batch jobs.
- **Embedded recent activity** means the home screen doesn't even read the `transactions` collection for the position — it's all in the profile document.
- **Budget alerts reuse the one read.** The same aggregate already returns `pfm_settings` and the consent-filtered accounts, so the budget spend is derived from that result (scope + definitions) plus a single `transactions` aggregate — the home screen is **one** `customer_profiles` read end-to-end, not a second consent-scope `find_one`.
- **Header is consistent with the cards.** The net position and the institution / account / consent counts are recomputed from the *same* consent-filtered accounts this read returns (`_live_summary`), not read from the materialized `summary`. So when a consent is revoked, the header updates in lock-step with the account cards on the very next read — it never lags the asynchronous `refresh_summary` that maintains the persisted `summary` (which still backs the customer switcher).

> 🎤 **Speaker note:** "Watch the `reads: 1`. That's the whole cross-bank financial picture in a *single* document read, with consent applied by the database itself. When I revoke a bank later in Scale Ops, this exact query stops returning those accounts on the next call — no cache to bust, no app flag to flip."

> 💡 **There are two enforcement paths.** This screen uses **Path A** — filter inside the single profile read. Transaction-level screens (next) use **Path B** — resolve the allowed account set from the same boxes, then every downstream query carries `{"account.account_id": {"$in": allowed}}`. Same source of truth, two shapes.

---

# b2. Transaction history + search

> 📋 **Open Finance DC API — addressed:** §5.7 Account Transactions (incl. the catch-all `description` / `recipient_reference` / `other_payment_description` fields) · §9 Pagination (opaque cursor, reused verbatim). Search itself is Acme value-add; its scope = §15 Consent Permission `read_transactions` + §5.2 Consented Accounts.
>
> 🍃 **MongoDB capability:** Atlas **Search** (full-text) + **Vector Search** (semantic) + **`$rankFusion`** hybrid — one engine, on the same documents, with consent on both legs. Flexible schema means those variable catch-all text fields are indexable with no migration.

### The story

The transaction list is a clean, filterable, **cursor-paginated** ledger across every consented institution. On top of it sits **search you can toggle between keyword and meaning**: type `coffee` and keyword search finds the literal string; type `my morning caffeine habit` and **vector search** finds Starbucks, the kopitiam, and the office café — because it searches on *meaning*, not text. The best results come from **hybrid** search, which fuses both.

### On screen

- **Transactions** tab: scroll the list, apply filters, click **Load more** (explicit pagination, not infinite scroll).
- The **search box** with a **Hybrid / Vector / Keyword** toggle. Try a literal merchant, then a fuzzy concept, then hybrid.

### Under the hood — the list

```text
GET /pfm/{customer_id}/transactions?cursor=…&page_size=50    →  PfmService.transactions()

route:    backend/acme_ofv/api/v1/routes/pfm.py:43–51
service:  backend/acme_ofv/services/pfm_service.py:235–298  (transactions)
```

Path B gate first (`allowed = require_scope(...)`), then a **covered, projected, cursor-paginated** find — equality/range on indexed fields only, **no regex**:

```python
match = {"customer_id": customer_id, "account.account_id": {"$in": allowed}, ...filters}
# keyset pagination on (transaction_date, _id) — stable, index-friendly, no big skips
```

### Under the hood — the search

```text
GET /pfm/{customer_id}/transactions/search?q=…&mode=hybrid   →  SearchService.search()

route:    backend/acme_ofv/api/v1/routes/pfm.py:54–66
service:  backend/acme_ofv/search/service.py:56–129  (search + fallback chain)
builders: backend/acme_ofv/search/pipeline_builders.py:25–82
```

The query is embedded with **Voyage `voyage-4-lite`** (sample customers only, to cap cost), and both legs are **consent-scoped** — the same allowed-account set, in two syntaxes:

```python
# backend/acme_ofv/search/pipeline_builders.py:25–35
def consent_search_filter(customer_id, allowed):     # text leg ($search compound filter)
    return [{"in": {"path": "customer_id", "value": [customer_id]}},
            {"in": {"path": "account.account_id", "value": list(allowed)}}]

def consent_vector_prefilter(customer_id, allowed):  # vector leg ($vectorSearch pre-filter)
    return {"customer_id": customer_id, "account.account_id": {"$in": list(allowed)}}
```

The hybrid pipeline runs **native `$rankFusion`** (MongoDB 8.1+) — text and vector in one pass, weighted, fused by reciprocal rank:

```python
# backend/acme_ofv/search/pipeline_builders.py:63–82 · build_hybrid_pipeline()
[
  {"$rankFusion": {
    "input": {"pipelines": {
      "textSearch":   [text_stage, {"$limit": fetch}],          # $search, fuzzy maxEdits:1
      "vectorSearch": [vector_stage],                            # $vectorSearch, kNN
    }},
    "combination": {"weights": {"vectorSearch": 0.7, "textSearch": 0.3}},
  }},
  {"$addFields": {"search_score": {"$meta": "score"}}},
  {"$skip": skip}, {"$limit": limit},
  {"$project": {"embedding": 0}},                                # never ship the vector
]
```

**Why it's built this way — the MongoDB value:**

- **One engine, both modalities.** Atlas Search (text) and Atlas Vector Search (semantic) live *inside* the database, on the same documents. No bolt-on search cluster, no separate vector DB, no sync job keeping three systems consistent.
- **`$rankFusion` fuses in one query.** You get keyword precision *and* semantic recall in a single round trip, weighted — instead of two queries merged in app code.
- **Consent applies to *both* legs.** The same allowed-account set is pushed into the `$search` compound `filter` and the `$vectorSearch` pre-filter, so semantic search can never surface a transaction from a revoked account.
- **Graceful degradation.** A four-step fallback chain (`$rankFusion → $vectorSearch → $search → app-side RRF`) means text search always returns, even on a pre-8.1 cluster or a customer with no embeddings.

> 🎤 **Speaker note:** "Type a concept, not a keyword — 'morning caffeine habit' — and it finds the café transactions with no matching text. That's vector search on the *same* documents, in the *same* database, with the *same* consent filter. No separate vector store to provision, secure, and keep in sync."

---

# c. Insights (Personal Financial Management)

> 📋 **Open Finance DC API — addressed:** consumes §5.6 Balance + §5.7 Transaction objects; §15 Enums (Account Category / Type / Subtype, Transfer Method) drive categorisation. The insights themselves are Acme value-add — the spec defines the *raw* objects, not the analytics over them.
>
> 🍃 **MongoDB capability:** the **aggregation framework** computes spend / cashflow / net-worth server-side (`$group`, `$topN`, `$dateTrunc`, `$cond`); a purpose-built **time-series collection** stores the balance history. No warehouse, no cube, no staleness.

### The story

Spend by category, money-in / money-out per month, net worth over time, recurring subscriptions. Every one of these is **computed by the database in a single aggregation** over the consented transactions — there's no analytics service, no pre-aggregated cube being kept in sync. The numbers are always live because they're computed from the source on read.

### On screen

- **Insights / Spend** — category breakdown with month-over-month deltas and top merchants per category.
- **Cashflow bars** — the money-in / money-out bars per month, split by institution (this powers the bar chart above the transaction list too).
- **Net worth** — assets vs liabilities trend, read from the `balance_snapshots` **time-series** collection.

### Under the hood — spend by category

```text
GET /pfm/{customer_id}/spend?month=YYYY-MM     →  PfmService.spend_by_category()

route:    backend/acme_ofv/api/v1/routes/pfm.py:14–17
service:  backend/acme_ofv/services/pfm_service.py:86–126
```

```python
# backend/acme_ofv/services/pfm_service.py:86–126 · spend_by_category() — group + top-N merchants, all in one pass
await aggregate_list(db.transactions, [
    {"$match": {"customer_id": customer_id,
                "account.account_id": {"$in": allowed},          # Path B consent scope
                "enrichment.month": month,                       # pre-computed at ingest
                "credit_debit_indicator": "debit",
                "is_settled": True,
                "enrichment.is_transfer_own_account": False}},   # exclude own-account shuffles
    {"$group": {
        "_id": "$enrichment.category",
        "total": {"$sum": "$amount.amount"},
        "count": {"$sum": 1},
        "top_merchants": {"$topN": {"n": 3, "sortBy": {"amount.amount": -1},
                          "output": {"m": "$enrichment.merchant_normalized", "a": "$amount.amount"}}},
    }},
    {"$sort": {"total": -1}},
])
# a second tiny aggregation on last month gives the month-over-month % delta
```

### Under the hood — cashflow (the money-in / money-out bars)

```text
GET /pfm/{customer_id}/cashflow?months=12      →  PfmService.cashflow()

route:    backend/acme_ofv/api/v1/routes/pfm.py:20–23
service:  backend/acme_ofv/services/pfm_service.py:128–168
```

```python
await aggregate_list(db.transactions, [
    {"$match": match},                                           # scope + month >= start
    {"$group": {                                                 # bucket by month × direction × institution
        "_id": {"month": "$enrichment.month", "ind": "$credit_debit_indicator",
                "institution": "$account.institution_name"},
        "total": {"$sum": "$amount.amount"}}},
    {"$group": {"_id": "$_id.month",
                "flows": {"$push": {"ind": "$_id.ind", "institution": "$_id.institution", "total": "$total"}}}},
    {"$sort": {"_id": 1}},
])
```

### Under the hood — net worth (time-series collection)

```text
GET /pfm/{customer_id}/net-worth?weeks=13      →  PfmService.net_worth()

route:    backend/acme_ofv/api/v1/routes/pfm.py:26–29
service:  backend/acme_ofv/services/pfm_service.py:170–197
```

```python
# backend/acme_ofv/services/pfm_service.py:170–197 · net_worth()
await aggregate_list(db.balance_snapshots, [                     # ← time-series collection
    {"$match": {"meta.customer_id": customer_id,
                "meta.account_id": {"$in": allowed},
                "as_of": {"$gte": start}}},
    {"$group": {"_id": {"week": {"$dateTrunc": {"date": "$as_of", "unit": "week"}},
                        "account_id": "$meta.account_id"},
                "indicator": {"$last": "$credit_debit_indicator"},
                "balance": {"$avg": {"$toDouble": "$current_balance"}}}},
    {"$group": {"_id": "$_id.week",
                "assets":      {"$sum": {"$cond": [{"$eq": ["$indicator", "credit"]}, "$balance", 0]}},
                "liabilities": {"$sum": {"$cond": [{"$eq": ["$indicator", "debit"]},  "$balance", 0]}}}},
    {"$project": {"net": {"$subtract": ["$assets", "$liabilities"]}, "assets": 1, "liabilities": 1}},
    {"$sort": {"_id": 1}},
])
```

**Why it's built this way — the MongoDB value:**

- **The database *is* the analytics engine.** `$group`, `$topN`, `$dateTrunc`, `$cond` — these run server-side, next to the data. There's no second system to feed, no staleness, no "the dashboard lags the ledger by a day."
- **Pre-computed `enrichment.month`/`category`** (stamped at ingest) means these aggregations match on indexed scalar fields — fast, no per-row date math, no regex.
- **A purpose-built time-series collection** (`balance_snapshots`) stores balance history compactly and makes windowed roll-ups (`$dateTrunc` to week) cheap. Balances are reconstructed from the real anchor + transaction deltas, so the trend is faithful.
- **Consent scope is in every `$match`** — insights can only ever aggregate over accounts the customer currently allows.

> 🎤 **Speaker note:** "Nobody pre-computed these numbers. There's no cube, no nightly rollup. The customer's category breakdown, their cashflow bars, their net-worth trend — all computed in the database, on read, over only the accounts they consent to. That's the difference between *storing* data in MongoDB and *running your application* on it."

---

# d. Underwriting

> 📋 **Open Finance DC API — addressed:** §15 Consent Purpose (a distinct credit-underwriting purpose) + §13 Consent Framework gate access; features are derived from §5.5 / §5.6 / §5.7 objects. The scorecard is Acme value-add.
>
> 🍃 **MongoDB capability:** the **aggregation pipeline** runs the scorecard in-database (`$facet` rollups, weighted `$add`/`$multiply`, `$switch` banding); a flexible feature document holds the per-account components.

> 💡 In the demo, press **"How it works"** on this screen for the live, collapsible version of the flow below; press **"⟷ operations performed"** on any result to see the *real* per-step MongoDB ops and timings captured by the Query Inspector.

### The story

A banker runs a credit inquiry for a personal loan. The platform builds a feature store **on demand**, computes a scorecard **inside an aggregation pipeline** over *only the accounts the customer consented for underwriting*, and writes an **immutable, consent-stamped decision** that reproduces forever. The same consolidated data that powers the customer's app powers the bank's credit decision — no data movement, no feature pipeline to maintain.

### On screen

- **Underwriting** console → **Run scorecard**. Watch the staged progress bar (gate → build features → salary → score → persist).
- A customer with **PFM-only** consent gets **HTTP 403** — same data, refused at the read path. No thin score, no silent fallback.

### Under the hood — the four operations of one inquiry

```text
POST /underwriting/{customer_id}/run     →  UnderwritingService.run()

route:    backend/acme_ofv/api/v1/routes/underwriting.py:11–15
service:  backend/acme_ofv/services/underwriting_service.py:158–223  (run)
          _salary_stats: lines 129–156   ·   score_pipeline: lines 18–122
```

**1) Consent gate** — resolve the `credit_underwriting` scope; `∅ → 403`.

**2) Build the feature store on demand** — reactive scoring: roll up 6-month per-account components from `transactions` + the reconstructed `balance_snapshots`, persist `uw_features`. *(A PFM-only customer never gets a feature doc until a credit inquiry runs.)*

**3) Salary statistics** — median monthly salary, regularity, payday from consented salary credits:

```python
# backend/acme_ofv/services/underwriting_service.py:129–156 · _salary_stats()
await aggregate_list(db.transactions, [
    {"$match": {"customer_id": customer_id, "account.account_id": {"$in": scope},
                "enrichment.category": "salary_income", "credit_debit_indicator": "credit",
                "transaction_date": {"$gte": now - 185d}}},
    {"$group": {"_id": "$enrichment.month",
                "total": {"$sum": {"$toDouble": "$amount.amount"}},
                "day":   {"$max": {"$dayOfMonth": "$transaction_date"}}}},
    {"$group": {"_id": None,
                "median_salary": {"$median": {"input": "$total", "method": "approximate"}},
                "stddev": {"$stdDevPop": "$total"}, "mean": {"$avg": "$total"},
                "months_observed": {"$sum": 1},
                "payday": {"$median": {"input": "$day", "method": "approximate"}}}},
])
# regularity = max(0, 1 − stddev/mean)  → income-stability signal
```

**4) Score inside the aggregation** — the entire scorecard is one `$facet` pipeline over `uw_features` (`score_pipeline()`):

<details>
<summary><b>Full scoring pipeline (expand)</b> — <code>backend/acme_ofv/services/underwriting_service.py:18–122 · score_pipeline()</code></summary>

```python
[
  {"$match": {"_id": customer_id}},
  {"$project": {"scoped": {"$filter": {                          # consent scope, enforced in-DB
      "input": "$accounts", "as": "a",
      "cond": {"$in": ["$$a.account_id", scope]}}}}},
  {"$facet": {                                                   # parallel rollups, one pass
      "flows":     [{"$unwind": "$scoped"}, {"$unwind": "$scoped.monthly"},
                    {"$group": {"_id": None, "inflow": {"$sum": {"$toDouble": "$scoped.monthly.inflow"}},
                                "outflow": {"$sum": {"$toDouble": "$scoped.monthly.outflow"}},
                                "gambling_6m": {"$sum": {"$toDouble": "$scoped.monthly.gambling_spend"}}}}],
      "credit":    [{"$unwind": "$scoped"}, {"$match": {"scoped.credit": {"$exists": True}}},
                    {"$group": {"_id": None, "total_limit": {"$sum": {"$toDouble": "$scoped.credit.limit"}},
                                "total_owed": {"$sum": {"$toDouble": "$scoped.credit.current_owed"}}}}],
      "loans":     [{"$unwind": "$scoped"}, {"$match": {"scoped.loan": {"$exists": True}}},
                    {"$group": {"_id": None, "installments": {"$sum": {"$toDouble": "$scoped.loan.installment"}}}}],
      "liquidity": [{"$unwind": "$scoped"}, {"$match": {"scoped.balance_stats_90d": {"$exists": True}}},
                    {"$group": {"_id": None, "avg_eod": {"$avg": {"$toDouble": "$scoped.balance_stats_90d.avg_eod"}},
                                "min_eod": {"$min": {"$toDouble": "$scoped.balance_stats_90d.min_eod"}},
                                "days_below_500": {"$max": "$scoped.balance_stats_90d.days_below_500"}}}],
  }},
  {"$addFields": {                                               # weighted scorecard, in-DB
      "rollup.utilization_now": {"$divide": ["$rollup.portfolio_owed", "$rollup.portfolio_limit"]},
      "rollup.dsr_estimate":    {"$divide": [{"$add": ["$rollup.loan_installments", "$rollup.card_min_payments"]}, salary]}}},
  {"$addFields": {"score": {"$round": [{"$add": [
      600,
      {"$multiply": [85, regularity]},
      {"$multiply": [70, {"$min": [{"$divide": ["$rollup.avg_eod", 5000]}, 1]}]},
      {"$multiply": [-85, "$rollup.utilization_now"]},
      {"$multiply": [-110, {"$min": ["$rollup.dsr_estimate", 1]}]},
      {"$multiply": [-90, {"$min": [{"$divide": ["$rollup.gambling_6m", 1000]}, 1]}]},
  ]}, 0]}}},
  {"$addFields": {                                               # band + decision via $switch
      "band":     {"$switch": {"branches": [{"case": {"$gte": ["$score", 740]}, "then": "A"}, ...], "default": "D"}},
      "decision": {"$switch": {"branches": [{"case": {"$gte": ["$score", 700]}, "then": "approve"},
                                            {"case": {"$gte": ["$score", 640]}, "then": "approve_with_conditions"}], "default": "decline"}}}},
]
```
</details>

**5) Persist an immutable run** — the decision is appended with verbatim copies of the governing consents and the exact features used:

```python
# backend/acme_ofv/services/underwriting_service.py:202–221 · run() → insert_one
await db.underwriting_runs.insert_one({
    "customer_id": customer_id, "run_at": now,
    "consent_snapshot": consent_snapshot,                        # verbatim governing consents
    "scope_account_ids": scope,
    "features_snapshot": {"components": components, "rollup": rollup, "derived": derived},
    "scorecard": {"score": ..., "band": ..., "decision": ..., "reason_codes": ..., "computed_in_db": True},
})
```

**Why it's built this way — the MongoDB value:**

- **The model runs *in* the data.** `$facet` computes flows, credit, loans and liquidity rollups in parallel, then `$add`/`$multiply`/`$switch` produce the score, band and decision — all server-side. `computed_in_db: true` is literal: the features never leave the database to be scored.
- **Reactive, on-demand features.** No nightly batch scoring millions of customers who'll never apply. The feature store is built exactly when an inquiry runs, over exactly the consented accounts.
- **Consent-stamped immutability = the audit story.** Each run embeds the consents that authorised it. Re-run it and you get the identical score. A customer who later revokes can't produce a *new* run — but this record stands, which is precisely what BNM/PDPA auditors ask for.

> 🎤 **Speaker note:** "The score is computed *inside* the database — `computed_in_db: true` isn't marketing, it's the pipeline. And every decision freezes the consent that justified it. If the regulator asks 'on what basis did you approve this loan in March?', the answer is one immutable document, reproducible byte-for-byte."

> 💡 **Objection — "Why not a feature store / ML platform?"** For population-scale, on-demand credit features sourced from the *same* transactional data, computing in-aggregation removes an entire data-movement + sync surface. When you do want a dedicated ML platform, the same documents feed it — this isn't either/or.

---

# e. Scale Ops — the revocation storm

> 📋 **Open Finance DC API — addressed:** §3.2 DC Consent Revocation Flow (client-credentials grant) · §4.7 Revocation · §5.4 Consent LCM (`/revoke` · `/suspend` · `/reactivate`, 201 → Account Access Consent Object) · §5.3 Webhook – Consent Event (`event_type` + `data`) · §13 Consent Framework (webhook notifications, status-reason rules) · §15 Enums (Consent Status / Status Reason / Event Type / Updated By). Plus the DC's PDPA erasure duty after revocation.
>
> 🍃 **MongoDB capability:** the **MongoDB Kafka Connect sink** lands every consent event from MSK into `consents`; **change streams** then drive the worker (no polling), a **multi-document ACID transaction** does the instant gate-flip, **chunked deletes off a covering index** erase without blocking reads, and a **reconciliation sweeper** makes erasure self-healing.

> 💡 In the demo, press **"How it works"** on the Revocation storm card for the live, collapsible version of this flow.

### The story

Consent isn't just an on-switch. Under PDPA/BNM, a revocation must take effect **immediately** for reads, and the data must then be **physically erased**. The provocative question is: *what happens when thousands revoke at once?* This screen fires **N concurrent revocations** through the real consent pipeline while a **steady read load** runs against One View — and shows that **reads don't flinch**. The gate flip is synchronous and ACID; the deletion is asynchronous and chunked.

### On screen

- **Scale Ops → Revocation storm → FIRE** (50 / 200 / 500 consents).
- Watch the **live pipeline feed** (change streams over SSE): `gate_flip` events, then `physical_erasure` events with batch metrics.
- Watch **read p50 / p99** stay flat in the storm result, and the **run history** table.

### Under the hood — the pipeline, end to end

```text
POST /ops/storm     →  OpsService.storm()  →  _run_storm()

route:    backend/acme_ofv/api/v1/routes/ops.py:23–27   (live SSE feed: /events lines 13–16)
service:  backend/acme_ofv/services/ops_service.py:94–114 (storm) · 116–178 (_run_storm)
producer: backend/acme_ofv/consent/producer.py:48–85   (publish_consent_event)
worker:   backend/acme_ofv/eraser/worker.py — gate_flip 71–100 · erase_consent_data 105–179
```

**1) Pick the cohort** — only consents the mock OFP actually knows about (so every revoke is honoured, not 404'd):

```python
# backend/acme_ofv/services/ops_service.py:100–103 · storm()
candidates = await db.consents.find(
    {"status": "authorized", "customer_id": {"$gte": "acme_cust_001000"}},
    {"consent_id": 1}).limit(n).to_list(None)
```

**2) Revoke concurrently through the real LCM endpoint** — exactly the call a customer tapping "revoke" makes (`asyncio.Semaphore(40)`):

```python
# backend/acme_ofv/services/ops_service.py:134–153 · _run_storm() → revoke_one()
r = await hc.post(f"/v1/consents/{c['consent_id']}/revoke",
                  json={"updated_by": "data_consumer_user"},
                  headers={"authorization": f"Bearer {token}"})
if r.status_code == 201:
    # deployed AWS backend runs transport=kafka → this PRODUCES to MSK:
    await publish_consent_event(db, r.json())     # the single ordered consent path
else:
    note_error(f"http_{r.status_code}: {r.text[:80]}")   # surfaced, not swallowed
```

**3) Publish to MSK — one ordered path** — the deployed AWS backend runs `CONSENT_EVENT_TRANSPORT=kafka`, so the post-image is **produced to the `rcp.consent.events` MSK topic**, keyed by `consent_id` (strict per-consent ordering on its partition) with a monotonic `_rcp_version`. Local dev uses `transport=direct` — the identical guarded upsert in-process:

```python
# backend/acme_ofv/consent/producer.py:48–85 · publish_consent_event()  (kafka 66–70 · guarded upsert 73–79)
doc["_rcp_version"] = int(updated_at.timestamp() * 1000)   # monotonic per consent_id

# DEPLOYED (AWS): transport == "kafka" → produce the post-image to MSK, keyed by consent_id:
await get_consent_producer().publish(s.kafka_consent_topic, value=doc, key=doc["_id"])
# value = Extended JSON (bson.json_util) → $date / $numberDecimal survive the wire

# LOCAL dev: transport == "direct" → the SAME guarded upsert, in-process:
db.consents.replace_one(
    {"_id": doc["_id"], "$or": [{"_rcp_version": {"$lt": doc["_rcp_version"]}},
                                {"_rcp_version": {"$exists": False}}]}, doc)
```

**3b) Kafka Connect sink → `consents` upsert** — the MongoDB Kafka Connect sink consumes the topic and applies the upsert into `acme_ofv.consents`, keyed by the `_id` carried in the value (= `consent_id`). It is the **only** writer of `consents`; nothing downstream knows whether the write came from Kafka or the in-process default:

```json
// ops/kafka/consents-mongo-sink.json · MongoSinkConnector
{
  "connector.class": "com.mongodb.kafka.connect.MongoSinkConnector",
  "topics": "rcp.consent.events",
  "database": "acme_ofv", "collection": "consents",
  "document.id.strategy": "...id.strategy.ProvidedInValueStrategy",   // _id = consent_id (from value)
  "writemodel.strategy": "...writemodel.strategy.ReplaceOneDefaultStrategy",
  "document.id.strategy.overwrite.existing": "true",
  "value.converter": "...StringConverter"            // Extended JSON → real Date / Decimal128
}
```

**4) Change-stream watcher → synchronous ACID gate-flip** — the eraser worker **tails the `acme_ofv.consents` change stream** (resume token persisted in `stream_checkpoints` → crash-resumable). When the sink writes the revoked post-image the watcher fires, flips the embedded boxes **and** writes the audit entry in **one multi-document ACID transaction**, then schedules erasure:

```python
# backend/acme_ofv/eraser/worker.py:311–333 (consents_watcher) · 85–116 (gate_flip)
async with await db.consents.watch(                        # the watcher that drives all of this
        [{"$match": {"operationType": {"$in": ["insert", "replace", "update"]}}}],
        resume_after=token) as stream:                     # resumable from stream_checkpoints
    async for change in stream:
        consent = change["fullDocument"]
        if consent["status"] in ("revoked", "expired", "suspended"):
            async with await sess.start_transaction():      # ACID: flip + audit commit together
                await db.customer_profiles.update_one(
                    {"_id": customer_id},
                    {"$set": {"accounts.$[a].consents.$[c].status": consent["status"]}},
                    array_filters=[{"a.consents.consent_id": consent_id}, {"c.consent_id": consent_id}])
                await db.consent_audit_log.insert_one({"action": "gate_flip", "status": consent["status"], ...})
            if consent["status"] == "revoked":
                asyncio.create_task(erase_consent_data(consent))     # → step 5
        await save_token("consents_watch", change["_id"])   # checkpoint → resumable
# from THIS commit, every read path (One View, Insights, search) excludes the accounts
```

> **Why is a profile-only flip enough?** Both read paths derive *live* consent from these embedded profile boxes — One View `$filter`s them in the read (Path A); transactions / insights / search / underwriting call `resolve_consent_scope()`, which reads `accounts.consents` to build the allowed-account set applied as `{"account.account_id": {"$in": allowed}}` (Path B). The `consents` collection is the event/ordering source + audit, *never* the request-time gate — so flipping the box here is the single enforcement switch; the transaction rows (which carry only a slim `consent_id` provenance stamp, not live status) are untouched until erasure.

**5) Asynchronous, chunked physical erasure** — only for accounts no longer covered by *any* other authorized consent (set difference); transactions deleted in **2,000-`_id` batches off the covering index** — no scans, no table locks — **idempotent + re-drivable** (each batch writes a heartbeat into the `erasure_job`):

```python
# backend/acme_ofv/eraser/worker.py:119–221 · erase_consent_data() + _drive_erasure()
job_id = (await db.erasure_jobs.insert_one(
    {"status": "running", "attempts": 1, "accounts": erase_set, ...})).inserted_id
while True:
    ids = [d["_id"] async for d in db.transactions.find(
        {"customer_id": customer_id, "account.account_id": {"$in": erase_set}},
        {"_id": 1}).limit(S.erase_batch_size)]                  # bounded batch, covering index
    if not ids:
        break
    res = await db.transactions.delete_many({"_id": {"$in": ids}})
    await db.erasure_jobs.update_one({"_id": job_id}, {
        "$inc": {"docs_deleted": res.deleted_count, "batches": 1},
        "$currentDate": {"updated_at": True}})                  # heartbeat → not seen as stale
db.balance_snapshots.delete_many({"meta.customer_id": cid, "meta.account_id": {"$in": erase_set}})
db.uw_features.update_one({"_id": cid}, {"$pull": {"accounts": {"account_id": {"$in": erase_set}}}})
await db.erasure_jobs.update_one({"_id": job_id}, {"$set": {"status": "completed", ...}})
# any throw → {"status": "failed", "last_error"} so the sweeper re-drives it
```

**6) Self-healing retry — the erasure sweeper** — erasure is fire-and-forget off the change stream (the resume token advances once the gate-flip commits), so a worker crash mid-delete would orphan rows. The sweeper is the **retry guarantee**: every 60 s it atomically **claims** any job left `running` with a stale heartbeat (worker died mid-delete) or `failed`, under an attempt ceiling, and resumes it — idempotently (it re-queries the *remaining* rows):

```python
# backend/acme_ofv/eraser/worker.py:224–246 · erasure_sweeper()
job = await db.erasure_jobs.find_one_and_update(
    {"status": {"$in": ["running", "failed"]},
     "attempts": {"$lt": ERASE_MAX_ATTEMPTS},
     "updated_at": {"$lt": now - ERASE_STALE_SECONDS}},      # stale heartbeat = died mid-flight
    {"$set": {"status": "running"}, "$inc": {"attempts": 1},
     "$currentDate": {"updated_at": True}},                  # the atomic update IS the lease
    return_document=ReturnDocument.AFTER)
if job:
    await _drive_erasure(job)   # re-queries REMAINING rows → idempotent, safe to retry
```

**7) Reads don't flinch** — a steady One View load samples p50 / p99 throughout the storm:

```python
# backend/acme_ofv/services/ops_service.py:155–161 · _run_storm() → read_load()
await aggregate_list(db.customer_profiles, one_view_pipeline(cid, "one_view"))   # single indexed read
# p99 stays flat: gate-flip is O(1) per consent, erasure is chunked, WiredTiger gives
# document-level concurrency — deletes and reads don't contend.
```

**Why it's built this way — the MongoDB value:**

- **Change streams turn the database into the event bus.** The worker reacts to consent state changes with no polling and no separate queue, and resume tokens make it crash-resumable.
- **Multi-document ACID transactions** make the gate flip atomic: the box update and the audit entry commit together, so from one instant the data is invisible to every read path — *regardless of how many rows still await deletion*.
- **Separation of "stop reading" from "delete"** is the key insight. Compliance requires reads to stop *now*; physical deletion of millions of rows can take seconds. By flipping the gate synchronously and erasing asynchronously in bounded batches, both are satisfied without ever locking the read path.
- **Self-healing erasure — retries, not transactions.** The bulk delete is *idempotent* and fire-and-forget, so a reconciliation **sweeper** re-drives any job left `running`/`failed` — that's the retry guarantee. Wrapping the delete in an ACID transaction would be the *wrong* tool: it would blow the 16 MB / 60 s transaction limits and hold a snapshot that stalls the read path. ACID is reserved for the small gate-flip; bulk erasure gets at-least-once + re-drive.
- **The deployed backend already runs Kafka.** `CONSENT_EVENT_TRANSPORT=kafka`: publish → MSK → Kafka Connect sink → `consents` → change stream → gate-flip → erasure. The only piece *not* built is the Acme-side upstream (`RCP MySQL → Debezium outbox → MSK`), which emits the byte-identical envelope — *nothing downstream of the topic changes*.

> 🎤 **Speaker note (the money line):** "Fire 500 revocations. Watch the read p99 line. It doesn't move. The gate flips in single-digit milliseconds inside an ACID transaction — reads stop instantly — and the actual deletion grinds through in 2,000-row batches in the background. That's how you do 'right to erasure' at population scale without taking the app down."

---

# The platform spine — one model, three audiences

| | Customer app | Acme banker | Platform / compliance |
|---|---|---|---|
| **Surface** | One View · Transactions · Insights | Underwriting | Scale Ops |
| **Read pattern** | 1 document / aggregations | feature aggregation | change streams + chunked deletes |
| **Consent** | enforced *inside* the read (Path A/B) | gate → `403` on ∅ | revoke → ACID flip → erase |
| **MongoDB value** | no joins, no ETL | model runs in the data | governance is a database property |

**The one-sentence close:** *The platform gives you the data; MongoDB is what lets you serve it to the customer, underwrite on it, and govern it — from a single, consolidated, document model, with consent enforced by the database itself.*

> 🎤 **Closing line:** "Same documents, three completely different audiences, one cluster. The customer's home screen, the bank's credit decision, and the regulator's erasure guarantee are all reads against the *same* model — and consent is a property of the data, not a flag in the app. That's the bet Open Finance asks you to make, and it's the bet MongoDB's document model is built for."

---

### Appendix · Code source map

All paths are relative to the repo root; backend code lives under `backend/acme_ofv/` (shown trimmed to `…/` below). Open the file at the line range and you land on the function whose snippet is shown in the section.

| Surface | Endpoint | Route (file:lines) | Service / pipeline (file:lines) |
|---|---|---|---|
| One View | `GET /one-view/{customer_id}` | `…/api/v1/routes/one_view.py:16–21` | `…/services/one_view_service.py:61–88` (budgets reuse the read; header recomputed live by `_live_summary:20–54`) · pipeline `…/consent/gate.py:26–62` |
| Customers list | `GET /customers` | `…/api/v1/routes/one_view.py:11–13` | `…/services/one_view_service.py:90–108` |
| Transactions | `GET /pfm/{customer_id}/transactions` | `…/api/v1/routes/pfm.py:43–51` | `…/services/pfm_service.py:235–298` |
| Search | `GET /pfm/{customer_id}/transactions/search` | `…/api/v1/routes/pfm.py:54–66` | `…/search/service.py:56–129` · builders `…/search/pipeline_builders.py:25–82` |
| Spend | `GET /pfm/{customer_id}/spend` | `…/api/v1/routes/pfm.py:14–17` | `…/services/pfm_service.py:86–126` |
| Cashflow | `GET /pfm/{customer_id}/cashflow` | `…/api/v1/routes/pfm.py:20–23` | `…/services/pfm_service.py:128–168` |
| Net worth | `GET /pfm/{customer_id}/net-worth` | `…/api/v1/routes/pfm.py:26–29` | `…/services/pfm_service.py:170–197` |
| Recurring | `GET /pfm/{customer_id}/recurring` | `…/api/v1/routes/pfm.py:32–34` | `…/services/pfm_service.py:199–218` (shared `recurring_groups_pipeline:53–77`) |
| Budgets | `GET /pfm/{customer_id}/budgets` (also served inline by One View) | `…/api/v1/routes/pfm.py:69–71` | `…/services/pfm_service.py:300–320` · `compute_budgets` (module fn, reused by One View) |
| Underwriting | `POST /underwriting/{customer_id}/run` | `…/api/v1/routes/underwriting.py:11–15` | `…/services/underwriting_service.py:158–223` · `score_pipeline 18–122` · `_salary_stats 129–156` |
| Revocation storm | `POST /ops/storm` | `…/api/v1/routes/ops.py:23–27` | `…/services/ops_service.py:94–114` (storm) · `116–178` (_run_storm) |
| Live feed (SSE) | `GET /ops/events` | `…/api/v1/routes/ops.py:13–16` | `…/services/ops_service.py:41–66` |
| Consent event path | _(every state change)_ | — | `…/consent/producer.py:48–85` (publish_consent_event) |
| Gate-flip + erasure | _(consents change stream)_ | — | `…/eraser/worker.py:71–100` (gate_flip) · `105–179` (erase_consent_data) |
| Ingestion backfill | _(authorized event)_ | — | `…/ingestion/backfill.py:26–158` (backfill_consent) · `170–240` (pull_transactions) |

*The browser calls the app endpoints via the `/api/*` dev-proxy prefix; the backend serves them at the paths above. The `/v1/...` OFP calls hit the Open Finance portal (mock at `…/mock_ofp/app.py`).*

---

### Appendix · Open Finance DC API — requirement coverage

§ references are to *Open Finance Platform — Data Consumer API*.

| Walkthrough area | the Open Finance platform spec sections addressed | Acme value-add beyond the spec |
|---|---|---|
| **a. Fetching data** | §3.1 + §3.4 flows · §4.3–§4.5 PAR/Auth/Token · §5.5–§5.7 Account/Balance/Transaction · §8 Custom Data · §9 Pagination · §10 Meta · §11 Security · §12 FAPI Headers | Write-time enrichment + idempotent consolidation into one model |
| **b. One View** | §5.2 Retrieve Consent · §13 Consent Framework · §15 Consent Status/Permission/Purpose | In-document consent gate; whole position in one indexed read |
| **b2. Transactions + search** | §5.7 Transactions · §9 Pagination · (scope: §15 Permission + §5.2) | Hybrid text + vector search on the same documents |
| **c. Insights** | §5.6 Balance · §5.7 Transaction · §15 Account Category/Type/Subtype, Transfer Method | Spend / cashflow / net-worth computed in-aggregation |
| **d. Underwriting** | §13 + §15 Consent Purpose; data from §5.5–§5.7 | Scorecard computed in-DB; immutable consent-stamped runs |
| **e. Scale Ops** | §3.2 Revocation Flow · §4.7 Revocation · §5.3 Webhook · §5.4 Consent LCM · §13 Framework · §15 Status/Reason/Event/Updated-By | Change-stream gate-flip + chunked erasure at population scale |
