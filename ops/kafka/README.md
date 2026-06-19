# MSK + Kafka Connect path (brief §7) — wiring notes

> STATUS: BUILT + DEPLOYED (2026-06-15). The consent-event Kafka pipe is live on
> AWS. See "Deployed estate" at the bottom. With `CONSENT_EVENT_TRANSPORT=kafka`
> every consent state change (link / revoke / suspend / reactivate / webhook +
> the revocation storm + the reauthorize simulation) is produced to MSK and
> applied to `acme_ofv.consents` by the Kafka Connect MongoDB sink — verified
> end-to-end (sink write -> change stream -> gate-flip / erasure).



The POC runs `CONSENT_EVENT_TRANSPORT=direct`, which applies the exact write
this sink would (ReplaceOneBusinessKey upsert keyed on `consent_id`, with the
`_rcp_version` guard handled by the producer). Moving to the real pipe is
infrastructure-only — nothing downstream of the topic changes.

## Estate (account <AWS_ACCOUNT_ID>, ap-southeast-1)

- MSK: `your-msk-cluster` (SASL/SCRAM :9096, IAM :9098)
- VPC `vpc-xxxxxxxx` (172.31.0.0/16) — Atlas peering `pcx-xxxxxxxx`
  already active (Atlas CIDR 192.168.248.0/21). NOTE: the brief's Connect host
  `i-xxxxxxxx` no longer exists — launch a fresh **t3.large** (Ubuntu,
  same subnets as the brokers).

## Steps

1. Topics (6 partitions, key = consent_id):
   `rcp.consent.events`, `acme.core.transactions` (12, key = customer_id),
   `ofp.webhook.events` (3).
2. EC2 t3.large: JDK 17 + Kafka 3.x, `mongodb-kafka-connector` 2.x jar in
   `plugin.path`. Distributed mode, single worker; config/offset/status topics
   on MSK; SCRAM creds + Atlas URI in `/opt/secrets.properties`
   (AWS Secrets Manager-sourced, same pattern as the reference estate).
3. Atlas IP access list: `172.31.0.0/16` via the existing peering; use the
   PrivateLink string `your-cluster-pl-0.xxxxx.mongodb.net` from inside the VPC.
4. `POST :8083/connectors` with `consents-mongo-sink.json`.
5. Switch the DC services: `CONSENT_EVENT_TRANSPORT=kafka`,
   `KAFKA_BOOTSTRAP=<broker list>` — the producer publishes the identical
   post-image JSON to the topic instead of upserting directly.

## Production upstream (not built — Acme's side)

Debezium MySQL source reading an RCP **consent outbox table** (full post-image
JSON per state change), routed by the `EventRouter` SMT to
`rcp.consent.events` — byte-identical envelope to what the POC producer emits,
monotonic `_rcp_version` assigned by the outbox.

---

## Deployed estate (2026-06-15, account <AWS_ACCOUNT_ID>, ap-southeast-1)

The whole backend stack runs on one EC2 in the MSK VPC, behind an ALB; only the
frontend stays local. Terraform: `deploy/terraform/`. The ALB is **TLS-only and
source-restricted** (Wiz remediation — see `docs/recent-work-5-aws-security-hardening.md`).

- ALB: `your-alb-dns.ap-southeast-1.elb.amazonaws.com`
  - `:443` -> DC API (`/healthz`, `/customers`, `/consents/...`, PFM, underwriting)
  - `:80`  -> 301 redirect to `:443`
  - `:8100` -> mock OFP (HTTPS)
  - SG ingress is locked to `var.allowed_cidrs` (not `0.0.0.0/0`); optional
    `X-API-Key` gate on the API (off unless `API_KEY` is set in the secret).
- EC2: `i-xxxxxxxx` (t3.large, public subnet `subnet-aaaaaaaa`), Docker
  compose runs `api:8010 · mock-ofp:8100 · eraser worker · kafka-connect:8083`.
  NOTE: the instance id changes whenever the AL2023 AMI rolls forward (apply
  replaces it) — don't hardcode it; resolve via `tag:Name=acme-ofv-app`.
- Deploy bucket: `s3://acme-ofv-deploy-<AWS_ACCOUNT_ID>/acme-ofv-deploy.tar.gz`
- Secret: `acme-ofv/dev` (ATLAS_URI PrivateLink, ATLAS_CERT_B64, VOYAGE_API_KEY).
- MSK: reused `your-msk-cluster` via IAM (instance role); SG rule added
  (app SG -> MSK `:9098`). Atlas: PrivateLink + X.509 (app PEM via pymongo;
  Connect via a JVM PKCS12 keystore generated at boot).

### Local frontend -> deployed ALB

The UI goes through the Vite dev proxy (server-side), so the browser stays on
`localhost` and never sees the ALB's self-signed cert:

```bash
# acme-ofv/frontend/.env
BACKEND_URL=https://your-alb-dns.ap-southeast-1.elb.amazonaws.com
VITE_API_URL=/api
API_KEY=          # only if the API gate is enabled (match the secret)
```
`vite.config.ts` proxies `/api` -> `BACKEND_URL` with `secure:false` (accepts the
self-signed cert) and injects `X-API-Key` when `API_KEY` is set. Your egress IP
must be in `allowed_cidrs`.

### Redeploy / update code (SSM, no SSH)

```bash
# repackage + push source
tar --exclude='backend/.venv' --exclude='*/__pycache__' -czf deploy/terraform/acme-ofv-deploy.tar.gz backend ops
aws s3 cp deploy/terraform/acme-ofv-deploy.tar.gz s3://acme-ofv-deploy-<AWS_ACCOUNT_ID>/acme-ofv-deploy.tar.gz
# on the instance (via: aws ssm send-command --instance-ids <current-id> --document-name AWS-RunShellScript ...)
aws s3 cp s3://acme-ofv-deploy-<AWS_ACCOUNT_ID>/acme-ofv-deploy.tar.gz /tmp/code.tar.gz && tar -xzf /tmp/code.tar.gz -C /opt/acme-ofv
cd /opt/acme-ofv/ops && docker compose -f docker-compose.aws.yml build && docker compose -f docker-compose.aws.yml up -d
```

### Gotchas hit + fixed (bake into any rebuild)

1. **VPC DNS returns NXDOMAIN for `*.mongodb.net`.** Public DNS resolves the
   PrivateLink `-pl-` SRV to the endpoint's private IPs. Fix: Docker daemon DNS
   = `8.8.8.8,1.1.1.1` (`/etc/docker/daemon.json`), so every container resolves
   Atlas (and MSK) correctly. Baked into `user_data.sh.tftpl`.
2. **Connect preflight (`cub kafka-ready`) couldn't find `IAMClientCallbackHandler`.**
   The MSK IAM jar must be on the cp preflight classpath too: copy it into
   `/usr/share/java/cp-base-new/` (not only `/usr/share/java/kafka/`).
   `Dockerfile.connect` does both.
3. **Sink wasn't persisting with `ReplaceOneBusinessKeyStrategy`.** Switched to
   `ReplaceOneDefaultStrategy` + `document.id.strategy.overwrite.existing=true`
   (upsert by the value's `_id` = consent_id). `consents-mongo-sink.json`.
4. **IMDS for container IAM creds**: launch template sets
   `http_put_response_hop_limit = 2` so containers reach IMDSv2 for MSK IAM.
5. **BSON fidelity**: producer emits Extended JSON (`bson.json_util`) and Connect
   uses `StringConverter`, so `$date`/`$numberDecimal` land as real Date/Decimal
   (not strings) — the gate's `expiration_datetime > now` keeps working.
