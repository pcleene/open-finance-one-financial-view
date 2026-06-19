"""Central configuration for every acme-ofv service (12-factor, .env-driven)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Atlas (X.509) ---
    atlas_uri: str = (
        "mongodb+srv://your-cluster.xxxxx.mongodb.net/"
        "?authSource=%24external&authMechanism=MONGODB-X509&appName=acme-ofv"
    )
    atlas_cert_path: str = str(REPO_ROOT / "secrets" / "atlas-x509.pem")

    # --- databases ---
    db_name: str = "acme_ofv"          # DC-side serving layer (the POC subject)
    mock_db_name: str = "ofp_mock"     # mock OFP's own store (stands in for the DPs)

    # --- service endpoints ---
    api_port: int = 8010               # DC service layer (one-view / pfm / uw / consent centre)
    mock_ofp_port: int = 8100          # Open Finance Platform mock (AS + RS)
    ofp_base_url: str = "http://127.0.0.1:8100"
    dc_base_url: str = "http://127.0.0.1:8010"   # webhook target {DC_URL}/v1/consents/events

    # --- Open Finance identifiers ---
    dc_id: str = "DC-ACME-001-2A8E"

    # --- API access control (defense-in-depth behind the ALB IP allow-list) ---
    # When non-empty, the DC API (and the mock's /admin/*) require header
    # `X-API-Key: <api_key>` on every request except /healthz (the ALB health
    # check cannot send custom headers). Empty (default) = disabled for local dev.
    api_key: str = ""
    # Fail closed: when True, a missing/empty `api_key` is treated as a
    # misconfiguration and gated requests are rejected (503) instead of silently
    # running open. Set AUTH_REQUIRED=true for any public/deployed environment.
    auth_required: bool = False
    # CORS allow-list (comma-separated origins) — pinned, not "*". The browser
    # only talks to the same-origin Vite dev proxy, so the real UI origins are
    # localhost; override CORS_ALLOW_ORIGINS for a deployed UI origin.
    cors_allow_origins: str = (
        "http://localhost:5173,http://localhost:5174,"
        "http://127.0.0.1:5173,http://127.0.0.1:5174"
    )

    # --- consent event transport: direct (Mongo sink emulation) | kafka (MSK) ---
    # In "kafka" mode every consent post-image is produced to the topic (keyed by
    # consent_id) and the MongoDB Kafka Connect sink applies the upsert; nothing
    # downstream of the topic changes (brief §7 / prompt-kafka-connect-...).
    consent_event_transport: str = "direct"
    kafka_bootstrap: str = ""
    kafka_consent_topic: str = "rcp.consent.events"
    # MSK auth: "msk_iam" (SASL_SSL + OAUTHBEARER via instance role, in-VPC) or
    # "plaintext" (local docker Kafka for off-VPC dev).
    kafka_auth: str = "msk_iam"
    kafka_consent_partitions: int = 6      # key = consent_id -> per-consent ordering
    kafka_consent_replication: int = 2     # cluster has 2 brokers
    aws_region: str = "ap-southeast-1"

    # --- ingestion knobs ---
    ofp_page_size: int = 500
    ofp_rate_limit_per_min: int = 200      # per DP, per spec
    incremental_overlap_days: int = 2

    # --- eraser knobs ---
    erase_batch_size: int = 2000
    eraser_concurrency: int = 32

    # --- mock crypto envelope (OFP_CRYPTO flag from the brief; off = readable JSON) ---
    ofp_crypto: str = "off"

    # --- transaction search / Voyage embeddings (brief §5) ---
    # shared key + embedding space with reference-demo: voyage-4-large indexes docs,
    # voyage-4-lite embeds queries (same space, comparable vectors).
    voyage_api_key: str = ""
    voyage_index_model: str = "voyage-4-large"
    voyage_query_model: str = "voyage-4-lite"
    voyage_rerank_model: str = "rerank-2.5"
    voyage_dimensions: int = 1024
    txn_search_index: str = "txn_text"
    txn_vector_index: str = "txn_vector"
    # vector cost guard: embed only the UI-switcher customers, capped total rows
    vector_sample_customer_count: int = 30   # mirrors /customers (header dropdown)
    vector_max_total: int = 5000             # hard ceiling on embedded transactions


@lru_cache
def settings() -> Settings:
    return Settings()
