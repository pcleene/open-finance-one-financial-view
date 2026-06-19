"""Open Finance Data Consumer API — enum quick reference (§15)."""

from enum import StrEnum


class ProviderStatus(StrEnum):
    online = "online"
    offline = "offline"


class ProviderType(StrEnum):
    bank = "bank"
    pension_fund = "pension_fund"


CONSENT_TYPE = "urn:openfinance-ml:account-access-consent:v1.2"


class ConsentPurpose(StrEnum):
    pfm = "pfm"
    credit_underwriting = "credit_underwriting"


class ConsentPermission(StrEnum):
    read_accounts = "read_accounts"
    read_transactions = "read_transactions"
    read_balances = "read_balances"


class ConsentStatus(StrEnum):
    awaiting_authorization = "awaiting_authorization"
    authorized = "authorized"
    failed = "failed"
    rejected = "rejected"
    suspended = "suspended"
    expired = "expired"
    revoked = "revoked"


# §13 lifecycle: allowed transitions; terminal states have no exits.
CONSENT_TRANSITIONS: dict[ConsentStatus, set[ConsentStatus]] = {
    ConsentStatus.awaiting_authorization: {
        ConsentStatus.authorized, ConsentStatus.failed, ConsentStatus.rejected,
    },
    ConsentStatus.authorized: {
        ConsentStatus.suspended, ConsentStatus.expired, ConsentStatus.revoked,
    },
    ConsentStatus.suspended: {
        ConsentStatus.authorized, ConsentStatus.revoked, ConsentStatus.expired,
    },
    ConsentStatus.failed: set(),
    ConsentStatus.rejected: set(),
    ConsentStatus.expired: set(),
    ConsentStatus.revoked: set(),
}

# Only these states may access data / count toward the uniqueness rule.
ACTIVE_CONSENT_STATES = {ConsentStatus.authorized, ConsentStatus.suspended}


class StatusReasonCode(StrEnum):
    inactive_account = "inactive_account"
    inactive_provider = "inactive_provider"
    not_allowed = "not_allowed"
    internal_error = "internal_error"
    format_error = "format_error"
    timeout = "timeout"
    provider_error = "provider_error"
    duplicate = "duplicate"
    validation_error = "validation_error"


class UpdatedBy(StrEnum):
    data_consumer = "data_consumer"
    data_consumer_user = "data_consumer_user"
    ofp = "ofp"
    ofp_user = "ofp_user"
    data_provider = "data_provider"
    data_provider_user = "data_provider_user"


class ConsentEventType(StrEnum):
    consent_status_updated = "consent_status_updated"


class AccountCategory(StrEnum):
    retail = "retail"
    corporate = "corporate"


class AccountType(StrEnum):
    deposit = "deposit"
    credit = "credit"
    loan = "loan"
    investment = "investment"
    ewallet = "ewallet"


class AccountSubtype(StrEnum):
    savings = "savings"
    current = "current"
    pension = "pension"
    credit_card = "credit_card"
    hire_purchase = "hire_purchase"
    mortgage = "mortgage"
    fund = "fund"
    others = "others"


TYPE_SUBTYPES: dict[AccountType, set[AccountSubtype]] = {
    AccountType.deposit: {AccountSubtype.savings, AccountSubtype.current, AccountSubtype.pension},
    AccountType.credit: {AccountSubtype.credit_card},
    AccountType.loan: {AccountSubtype.hire_purchase, AccountSubtype.mortgage},
    AccountType.investment: {AccountSubtype.fund},
    AccountType.ewallet: {AccountSubtype.others},
}


class IdType(StrEnum):
    nric = "nric"
    passport = "passport"


class CreditDebitIndicator(StrEnum):
    credit = "credit"
    debit = "debit"


class TransferMethod(StrEnum):
    funds_transfer = "funds_transfer"
    online_payment = "online_payment"
    recurring_payment = "recurring_payment"
    bill_payment = "bill_payment"
    instore_payment = "instore_payment"
    cheque = "cheque"
    cash_withdrawal = "cash_withdrawal"
    cash_deposit = "cash_deposit"
    others = "others"


TRANSFER_SUBMETHODS: dict[TransferMethod, list[str]] = {
    TransferMethod.funds_transfer: [
        "duitnow_transfer", "intrabank", "bank_adjustment", "ibg",
        "shared_atm_network_ibft", "rtgs", "others",
    ],
    TransferMethod.online_payment: [
        "fpx", "obw", "duitnow_pay", "debit_card_not_present", "others",
    ],
    TransferMethod.recurring_payment: ["direct_debit", "auto_debit", "others"],
    TransferMethod.bill_payment: ["jompay", "others"],
    TransferMethod.instore_payment: ["duitnow_qr", "debit_card", "others"],
    TransferMethod.cheque: ["espick", "others"],
    TransferMethod.cash_withdrawal: [
        "shared_atm_network", "mydebit_cash_out", "dnqr_cash_out", "others",
    ],
    TransferMethod.cash_deposit: ["shared_atm_network", "others"],
    TransferMethod.others: ["others"],
}
