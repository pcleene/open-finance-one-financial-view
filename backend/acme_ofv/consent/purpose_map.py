"""Internal→Open Finance purpose taxonomy (brief §5.1) — configuration, not code.

'One Financial View' is a Acme-internal purpose riding on `pfm` consent
(the platform's enum is fixed: pfm, credit_underwriting). Adding a future internal
purpose (e.g. wealth_advisory) is one line here.
"""

INTERNAL_TO_OFP_PURPOSE: dict[str, list[str]] = {
    "one_view":            ["pfm"],
    "pfm":                 ["pfm"],
    "credit_underwriting": ["credit_underwriting"],
}

PERMISSION_REQUIRED: dict[str, str] = {
    "one_view":            "read_balances",       # minimum to render the dashboard
    "pfm":                 "read_transactions",
    "credit_underwriting": "read_transactions",
}

# Whether Acme-internal (core banking) accounts ride the gate without an OFP
# consent. One View / PFM: yes — it's the customer's own dashboard. Credit
# underwriting: no — a run must be backed by explicit credit_underwriting
# consent, so a pfm-only customer resolves to ∅ scope → 403, never a thin score.
INTERNAL_DATA_IN_SCOPE: dict[str, bool] = {
    "one_view":            True,
    "pfm":                 True,
    "credit_underwriting": False,
}


def internal_purposes_for(ofp_purpose: str) -> list[str]:
    """Reverse mapping: which internal purposes does a the Open Finance platform consent unlock."""
    return [k for k, v in INTERNAL_TO_OFP_PURPOSE.items() if ofp_purpose in v]
