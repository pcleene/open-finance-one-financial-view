"""Open Finance Platform v1.2.2 resource objects (§5) — Pydantic v2, single source of schema truth.

These models are used by BOTH the OFP mock (response models) and the DC-side
ingestion service (parsing/validation), so a field drift breaks loudly at dev
time, not in a demo.

Spec amounts are strings matching ^\\d{1,13}\\.\\d{2}$ on the wire; the DC
converts to Decimal128 at the ingestion boundary (never float).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from acme_ofv.ofp_models.enums import (
    CONSENT_TYPE,
    AccountCategory,
    AccountSubtype,
    AccountType,
    ConsentEventType,
    ConsentPermission,
    ConsentPurpose,
    ConsentStatus,
    CreditDebitIndicator,
    IdType,
    ProviderStatus,
    ProviderType,
    StatusReasonCode,
    TransferMethod,
    UpdatedBy,
)

AMOUNT_RE = re.compile(r"^\d{1,13}\.\d{2}$")


class OFPModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Amount(OFPModel):
    amount: str
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("amount")
    @classmethod
    def _pattern(cls, v: str) -> str:
        if not AMOUNT_RE.match(v):
            raise ValueError(f"amount must match ^\\d{{1,13}}\\.\\d{{2}}$, got {v!r}")
        return v


class BalanceAmount(Amount):
    credit_debit_indicator: CreditDebitIndicator


class StatusReason(OFPModel):
    reason_code: StatusReasonCode
    reason_description: str | None = None


def _validate_custom_data(v: dict[str, str] | None) -> dict[str, str] | None:
    if v is None:
        return v
    if len(v) > 20:
        raise ValueError("custom_data limited to 20 key-value pairs")
    for k, val in v.items():
        if len(k) > 50 or len(str(val)) > 300:
            raise ValueError("custom_data key<=50 / value<=300 chars")
    return v


# ---------------------------------------------------------------- providers

class Provider(OFPModel):
    provider_id: str = Field(max_length=36)
    name: str
    status: ProviderStatus
    provider_type: ProviderType
    authorization_server_url: str
    resource_server_url: str
    supported_use_cases: list[ConsentPurpose]


# ----------------------------------------------------------------- consents

class ConsentedAccount(OFPModel):
    account_id: str = Field(max_length=36)
    account_number: str  # masked first6+last4 for credit types
    account_name: str


class AccountAccessConsent(OFPModel):
    """§5.2 Account Access Consent Object — webhook, GET, LCM response, token authorization_details."""

    consent_id: str = Field(max_length=36)
    dc_id: str = Field(max_length=36)
    dp_id: str = Field(max_length=36)
    id_type: IdType | None = None              # C: required when authorized
    hashed_id_number: str | None = Field(default=None, min_length=44, max_length=44)
    consent_type: str = CONSENT_TYPE
    consent_purpose: ConsentPurpose
    permissions: list[ConsentPermission]
    expiration_datetime: datetime
    status: ConsentStatus
    status_reason: StatusReason | None = None  # C: mandatory rejected/suspended
    accounts: list[ConsentedAccount] | None = None  # C: present once authorized
    created_at: datetime
    updated_at: datetime | None = None
    updated_by: UpdatedBy | None = None

    @model_validator(mode="after")
    def _status_reason_rules(self) -> "AccountAccessConsent":
        st = ConsentStatus(self.status)
        if st in (ConsentStatus.rejected, ConsentStatus.suspended) and self.status_reason is None:
            raise ValueError(f"status_reason mandatory for {st}")
        if st == ConsentStatus.authorized and self.status_reason is not None:
            raise ValueError("status_reason must be cleared on transition back to authorized")
        return self


class ConsentEvent(OFPModel):
    """Webhook payload: POST {DC_URL}/v1/consents/events."""

    event_type: ConsentEventType = ConsentEventType.consent_status_updated
    consent: AccountAccessConsent


# ----------------------------------------------------------------- accounts

class LoanDetails(OFPModel):
    loan_amount: Amount
    origination_date: date
    maturity_date: date


class Account(OFPModel):
    """§5.5 Account Object with R/O/C conditionality by type."""

    account_id: str = Field(max_length=36)
    account_number: str
    account_name: str
    account_holder_name: str
    institution_name: str
    category: AccountCategory
    type: AccountType
    subtype: AccountSubtype
    loan_details: LoanDetails | None = None
    limit: Amount | None = None
    interest_rate: float | None = None
    minimum_payment_amount: Amount | None = None
    payment_due_date: date | None = None
    custom_data: dict[str, str] | None = None

    _cd = field_validator("custom_data")(_validate_custom_data)

    @model_validator(mode="after")
    def _conditionals(self) -> "Account":
        t = AccountType(self.type)
        if t == AccountType.loan and self.loan_details is None:
            raise ValueError("loan_details required when type=loan")
        if t in (AccountType.credit, AccountType.loan):
            missing = [
                f for f in ("interest_rate", "minimum_payment_amount", "payment_due_date")
                if getattr(self, f) is None
            ]
            if missing:
                raise ValueError(f"{missing} required for {t} accounts")
        if t == AccountType.credit and self.limit is None:
            raise ValueError("limit required for credit accounts")
        return self


# ----------------------------------------------------------------- balances

class Balance(OFPModel):
    """§5.6 Balance Object — point-in-time only (no history endpoint in v1.2.x)."""

    account_id: str
    current_balance: BalanceAmount
    available_balance: BalanceAmount | None = None  # C: required deposit/credit
    statement_balance: BalanceAmount | None = None
    credit_lines_included: bool
    statement_date: date | None = None
    custom_data: dict[str, str] | None = None

    _cd = field_validator("custom_data")(_validate_custom_data)


# ------------------------------------------------------------- transactions

class Transaction(OFPModel):
    """§5.7 Transaction Object — R/O/C polymorphism by account type."""

    account_id: str
    transaction_id: str
    transaction_date: datetime
    credit_debit_indicator: CreditDebitIndicator
    amount: Amount
    foreign_currency_amount: Amount | None = None    # C: FX rows
    transfer_method: TransferMethod | None = None    # C: deposit accounts
    transfer_submethod: str | None = None            # C: deposit accounts
    description: str = Field(max_length=300)
    recipient_reference: str | None = Field(default=None, max_length=300)
    other_payment_description: str | None = Field(default=None, max_length=300)
    merchant_name: str | None = Field(default=None, max_length=100)  # C: credit_card/deposit
    mcc: str | None = Field(default=None, max_length=4)              # O: credit_card
    is_settled: bool
    custom_data: dict[str, str] | None = None

    _cd = field_validator("custom_data")(_validate_custom_data)


# ------------------------------------------------------------- pagination

class PageMeta(OFPModel):
    """Cursor-only pagination (v1.2.x): opaque `after` cursor replayed verbatim."""

    next_page_params: str | None = None


class ProvidersResponse(OFPModel):
    data: list[Provider]
    meta: PageMeta = PageMeta()


class TransactionsResponse(OFPModel):
    data: list[Transaction]
    meta: PageMeta = PageMeta()


# ------------------------------------------------------------------ errors

class OFPError(OFPModel):
    code: str          # e.g. Consent.Invalid, Pagination.InvalidCursor
    message: str
    details: Any | None = None


class OFPErrorResponse(OFPModel):
    errors: list[OFPError]
