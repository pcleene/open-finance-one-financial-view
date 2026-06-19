# Lucidchart diagramming brief — Acme Open Finance (One Financial View)

A draw-ready brief for building architecture / system-design diagrams. Each diagram
below lists its **purpose**, **swimlanes/containers**, **nodes** (with suggested
shape), and **edges** (directed, labeled). It is environment-agnostic — no secrets or
specific resource identifiers. Pair this with `ARCHITECTURE.md` for narrative.

**Suggested visual language (keep consistent across diagrams)**
- Containers/zones: rounded rectangles, light fill, labeled header (Client / Data
  Consumer / OFP / Data / AWS).
- Services/processes: rectangles. Datastores: cylinders. Topics/queues: parallelogram
  or "queue" shape. External actors/users: person icon. Async/stream edges: dashed.
  Sync/request edges: solid. Label every edge with the protocol or action.
- Color hints: Consumer = blue, OFP/providers = amber, data = green, AWS infra =
  grey, governance/erasure = red accents.
- Number the steps on flows (1, 2, 3...) so the diagram reads in order.

---

## Diagram 1 — Global system context (the "one-slide" overview)

**Purpose:** show the whole system and the main flows at a glance.

**Swimlanes / containers**
- `Client`
- `Data Consumer (Acme)`
- `Open Finance Platform + Data Providers (mocked)`
- `MongoDB Atlas`

**Nodes**
- Client: `SvelteKit UI` (rectangle) — sublabel "One View · PFM · Search · Underwriting · Profile · Staff tools".
- Data Consumer: `DC API (FastAPI)` (rectangle); `Worker (change-stream consumer)` (rectangle).
- OFP: `Mock OFP — Auth Server + Resource Server + DP data` (rectangle).
- Atlas: `acme_ofv (serving layer)` (cylinder); `ofp_mock (provider data)` (cylinder).

**Edges**
1. `SvelteKit UI` → `DC API`: solid, "HTTPS JSON".
2. `DC API` → `acme_ofv`: solid, "consent-gated reads / writes".
3. `DC API` → `Mock OFP`: solid, "PAR / authorize / token / data pull (rate-limited)".
4. `Mock OFP` → `ofp_mock`: solid, "provider data".
5. `DC API` → `acme_ofv (consents)`: solid, "publish consent event".
6. `acme_ofv` ⇢ `Worker`: dashed, "change stream".
7. `Worker` → `acme_ofv`: solid, "gate-flip / erase / backfill".
8. `Worker` → `Mock OFP`: solid, "backfill pulls".

---

## Diagram 2 — Consent lifecycle (sequence)

**Purpose:** authorization grant + revocation, end to end. Use a **sequence diagram**.

**Lifelines (left→right):** `Customer (UI)`, `DC API`, `Mock OFP (AS)`,
`consents (registry)`, `Worker`, `acme_ofv`.

**Messages — grant**
1. Customer → DC API: "link institution".
2. DC API → Mock OFP: "PAR".
3. DC API → Customer: "authorize_url".
4. Customer → Mock OFP: "authorize (approve accounts)".
5. Mock OFP → DC API: "redirect + code".
6. DC API → Mock OFP: "token exchange".
7. DC API → consents: "publish post-image (status=authorized)".
8. consents ⇢ Worker: "change stream".
9. Worker → Mock OFP: "backfill pulls (accounts/balances/txns)".
10. Worker → acme_ofv: "upsert txns + embed accounts + snapshots + summary".

**Messages — revoke** (separate block, red accent)
11. Customer → DC API: "revoke".
12. DC API → Mock OFP: "revoke (LCM)".
13. DC API → consents: "publish post-image (status=revoked)".
14. consents ⇢ Worker: "change stream".
15. Worker → acme_ofv: "gate-flip (ACID, instant)".
16. Worker → acme_ofv: "chunked physical erasure (accounts not covered by another consent)".

**Annotation:** callout box — "Enforcement is at the read path; the gate-flip stops
reads instantly, erasure follows asynchronously."

---

## Diagram 3 — Ingestion + balance reconstruction

**Purpose:** how a consent grant becomes stored, enriched data + a balance time series.

**Nodes (flowchart, left→right)**
- `Consent authorized` (start/rounded).
- `backfill_consent` (process).
- `Pull account objects` (process).
- `Pull balance (point-in-time)` (process) → `Anchor snapshot` (small note).
- `Pull transactions (cursor paged, 429 backoff)` (process).
- `Enrich + idempotent upsert` (process).
- `reconstruct_snapshot_history` (process, highlight).
- `balance_snapshots (EOD time series)` (cylinder).
- `Embed accounts + refresh summary + detect recurring` (process).

**Edges**
1. `Consent authorized` → `backfill_consent`.
2. `backfill_consent` → `Pull account objects`.
3. `backfill_consent` → `Pull balance` → "anchor".
4. `backfill_consent` → `Pull transactions` → `Enrich + idempotent upsert`.
5. `Pull balance` + `Pull transactions` → `reconstruct_snapshot_history` (two inbound edges).
6. `reconstruct_snapshot_history` → `balance_snapshots`, label "walk txns backward from anchor".
7. `backfill_consent` → `Embed accounts + refresh summary + detect recurring`.

**Callout:** "OFP serves balances point-in-time only; the EOD series is reconstructed
from real transaction deltas, not synthesized. Runs once per consent's first backfill."

---

## Diagram 4 — Read/serving paths (3 mini-flows, one canvas)

**Purpose:** the three signature reads. Lay out as three labeled lanes.

**Lane A — One View (Path A, single read)**
- `UI` → `DC API` → `customer_profiles` (cylinder). Edge label "1 aggregation, consent
  $filter on embedded accounts + recent txns". Callout: "the whole position is ONE
  `customer_profiles` read; home budget alerts reuse that read's resolved scope + one
  `transactions` aggregate — no second profile read."

**Lane B — Hybrid transaction search**
- `query text` → `Atlas Search ($search full-text)` (process).
- `query text` → `Embed query (Voyage)` → `Vector Search ($vectorSearch)` (process).
- both → `$rankFusion (weighted)` (process) → `Consent-filtered results` (output).
- Callout: "both legs apply the consent filter as a hard constraint; embeddings capped
  to a sample of customers."

**Lane C — Underwriting**
- `Consent gate (credit_underwriting scope; 403 if empty)` → `Build feature store
  (on demand)` → `Salary statistics` → `Score inside $facet aggregation` →
  `Persist immutable underwriting_run (consent-stamped)`.
- Callout: "feature store built reactively at inquiry; each step records latency +
  the MongoDB ops it ran (progress popup)."

---

## Diagram 5 — Consent-event transport: direct vs kafka

**Purpose:** show the two interchangeable transports end with the same upsert.

**Container A — `transport = direct (default, zero infra)`**
- `publish_consent_event` (process) → `consents` (cylinder), edge "guarded upsert".

**Container B — `transport = kafka (production-shaped)`**
- `publish_consent_event` (process) → `Kafka topic (key = consent_id)` (queue shape),
  edge "produce post-image (Extended JSON)".
- `Kafka topic` → `Kafka Connect MongoDB sink` (process) → `consents` (cylinder),
  edge "upsert (string converter -> real BSON types)".

**Shared bottom**
- `consents` ⇢ `Worker` (both containers point to one Worker), dashed "change stream".

**Callout:** "Keyed by consent_id ⇒ per-consent ordering. Downstream of the registry,
nothing changes between transports."

---

## Diagram 6 — AWS deployment topology

**Purpose:** the deployed shape (generic — no DNS/IDs).

**Swimlanes / containers**
- `Operator laptop`
- `AWS VPC (shared with the managed Kafka cluster)`
- `MongoDB Atlas` (outside the VPC, reached privately)

**Nodes**
- Laptop: `SvelteKit dev server` (rectangle), sublabel "proxies /api → ALB".
- VPC: `Application Load Balancer` (rectangle); `EC2 — Docker compose: API · mock OFP
  · worker · Kafka Connect` (rectangle, large); `Managed Kafka (IAM auth)` (queue/cluster shape).
- Atlas: `MongoDB Atlas cluster` (cylinder).

**Edges**
1. `SvelteKit dev server` → `Application Load Balancer`: solid, "HTTP (dev proxy)".
2. `Application Load Balancer` → `EC2`: solid, "forward :80 / :8100".
3. `EC2` → `Managed Kafka`: solid, "IAM SASL produce/consume".
4. `Managed Kafka` → `EC2`: dashed, "Kafka Connect sink consumes".
5. `EC2` → `Atlas`: solid, "X.509 over PrivateLink".

**Callout:** "Backend runs in the Kafka cluster's VPC; Atlas reached privately over
PrivateLink with X.509. Only the frontend stays local."

---

## Diagram 7 — Worker reactions (state → action)

**Purpose:** what the change-stream worker does per consent status. Use a decision/
branch layout.

**Nodes**
- `consents change event` (start).
- Decision diamond: `status?`
- Branches:
  - `authorized` → `backfill (first time)` / `re-project consent boxes (reactivation)`.
  - `suspended / expired` → `gate-flip (ACID: box update + audit)`.
  - `revoked` → `gate-flip` → `chunked physical erasure (set difference)` →
    `erasure_jobs metrics`.
- Side nodes: `live uw_features updater (per-insert bucket inc)`; `expiry sweeper`.

**Callout:** "Sole writer of consent boxes into the profile; change-stream resume
tokens persisted ⇒ crash-resumable."

---

## Optional polish
- Add a small legend (solid = sync request, dashed = async/stream; shape key).
- A "principles" side panel on Diagram 1: consent enforced at read; one ordered path
  per consent; single-read One View; lean rows; reactive underwriting.
- Keep Diagram 1 as the hero; Diagrams 2–7 as detail pages in the same Lucidchart doc.
