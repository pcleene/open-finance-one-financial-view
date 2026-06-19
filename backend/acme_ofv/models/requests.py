"""Typed request bodies — replace `await request.json()` + body["x"] access."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BudgetItem(BaseModel):
    category: str
    monthly_limit: float
    alert_threshold: float = 0.8


class BudgetsRequest(BaseModel):
    budgets: list[BudgetItem] = Field(default_factory=list)


class LinkRequest(BaseModel):
    dp_id: str
    consent_purpose: str
    permissions: list[str]
    validity_days: int = 180
    # Browser-facing bases so a local UI behind a dev proxy can reach the OFP
    # authorize page + the DC callback. Default (None) → server-internal URLs
    # (the fully-local setup). authorize_base="" yields a same-origin relative
    # URL (the proxied AWS setup).
    authorize_base: str | None = None
    redirect_base: str | None = None


class StormRequest(BaseModel):
    n: int = 200
    read_rps: int = 50


class SimulationRequest(BaseModel):
    count: int = 200          # authorized consents to drive
    concurrency: int = 8      # bounded in-flight pulls
    # "incremental" = re-pull existing consents; "reauthorize" = revoke -> erase
    # -> re-link -> backfill (fires brand-new consents through the real LCM path)
    mode: str = "incremental"
