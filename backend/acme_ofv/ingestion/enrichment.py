"""Ingestion-time enrichment — categorizer v3 (brief §9).

All categorization happens HERE, at write time, in Python:
MCC map ∪ transfer_submethod map ∪ merchant dictionary (exact normalized-name
lookups). No regex ever reaches a database query.
"""

from datetime import datetime, timedelta, timezone

CATEGORIZER_VERSION = 4

MYT = timezone(timedelta(hours=8))

CATEGORIES = [
    "food_and_beverage", "groceries", "transport", "fuel", "shopping",
    "entertainment", "bills_utilities", "telco_internet", "health", "education",
    "travel", "insurance", "financial_services", "cash", "transfers",
    "salary_income", "investment", "npf_contribution", "loan_repayment",
    "gambling", "uncategorized",
]

# ---- MCC → category (subset of the ~300-code map; demo-relevant codes) ----
MCC_MAP: dict[str, str] = {
    "5811": "food_and_beverage", "5812": "food_and_beverage", "5813": "food_and_beverage",
    "5814": "food_and_beverage", "5499": "groceries", "5411": "groceries",
    "5912": "health", "8011": "health", "8021": "health", "8062": "health",
    "4111": "transport", "4121": "transport", "4789": "transport",
    "5541": "fuel", "5542": "fuel",
    "5311": "shopping", "5651": "shopping", "5691": "shopping", "5732": "shopping",
    "5942": "shopping", "5999": "shopping", "5945": "shopping",
    "7832": "entertainment", "7841": "entertainment", "7922": "entertainment",
    "7994": "entertainment", "5816": "entertainment",
    "4900": "bills_utilities",
    "4814": "telco_internet", "4816": "telco_internet", "4899": "telco_internet",
    "8211": "education", "8220": "education", "8299": "education",
    "3000": "travel", "4511": "travel", "7011": "travel", "4722": "travel",
    "6300": "insurance", "5960": "insurance",
    "6011": "cash", "6012": "financial_services", "6051": "financial_services",
    "6211": "investment", "6540": "financial_services",
    "7995": "gambling",
}

# ---- transfer_submethod → default category (deposit accounts) ----
SUBMETHOD_MAP: dict[str, str] = {
    "jompay": "bills_utilities",
    "direct_debit": "bills_utilities",
    "auto_debit": "bills_utilities",
    "shared_atm_network": "cash",
    "mydebit_cash_out": "cash",
    "dnqr_cash_out": "cash",
    "duitnow_transfer": "transfers",
    "intrabank": "transfers",
    "ibg": "transfers",
    "rtgs": "transfers",
    "shared_atm_network_ibft": "transfers",
    "bank_adjustment": "financial_services",
}

# ---- merchant dictionary: normalized key → (display name, category, subcategory) ----
# Curated for the demo dataset; exact-match lookups only (built offline in prod).
MERCHANT_DICT: dict[str, tuple[str, str, str]] = {
    "tealive": ("Tealive", "food_and_beverage", "coffee"),
    "zus coffee": ("ZUS Coffee", "food_and_beverage", "coffee"),
    "mcdonalds": ("McDonald's", "food_and_beverage", "fast_food"),
    "kfc": ("KFC", "food_and_beverage", "fast_food"),
    "mamak maju": ("Restoran Mamak Maju", "food_and_beverage", "restaurant"),
    "old town white coffee": ("OldTown White Coffee", "food_and_beverage", "cafe"),
    "grabfood": ("GrabFood", "food_and_beverage", "delivery"),
    "foodpanda": ("foodpanda", "food_and_beverage", "delivery"),
    "lotuss": ("Lotus's", "groceries", "supermarket"),
    "jaya grocer": ("Jaya Grocer", "groceries", "supermarket"),
    "99 speedmart": ("99 Speedmart", "groceries", "convenience"),
    "aeon": ("AEON", "groceries", "supermarket"),
    "village grocer": ("Village Grocer", "groceries", "supermarket"),
    "grab": ("Grab", "transport", "ride_hailing"),
    "touch n go": ("Touch 'n Go", "transport", "toll_transit"),
    "rapidkl": ("RapidKL", "transport", "public_transit"),
    "petronas": ("PETRONAS", "fuel", "petrol_station"),
    "shell": ("Shell", "fuel", "petrol_station"),
    "petron": ("Petron", "fuel", "petrol_station"),
    "shopee": ("Shopee", "shopping", "ecommerce"),
    "lazada": ("Lazada", "shopping", "ecommerce"),
    "uniqlo": ("UNIQLO", "shopping", "apparel"),
    "mr diy": ("MR DIY", "shopping", "home"),
    "zara": ("ZARA", "shopping", "apparel"),
    "netflix": ("Netflix", "entertainment", "streaming"),
    "spotify": ("Spotify", "entertainment", "streaming"),
    "gsc": ("Golden Screen Cinemas", "entertainment", "cinema"),
    "tgv": ("TGV Cinemas", "entertainment", "cinema"),
    "steam": ("Steam", "entertainment", "gaming"),
    "disney plus": ("Disney+", "entertainment", "streaming"),
    "tnb": ("Tenaga Nasional", "bills_utilities", "electricity"),
    "air selangor": ("Air Selangor", "bills_utilities", "water"),
    "indah water": ("Indah Water", "bills_utilities", "sewerage"),
    "unifi": ("Unifi", "telco_internet", "broadband"),
    "maxis": ("Maxis", "telco_internet", "mobile"),
    "celcomdigi": ("CelcomDigi", "telco_internet", "mobile"),
    "guardian": ("Guardian", "health", "pharmacy"),
    "watsons": ("Watsons", "health", "pharmacy"),
    "klinik mediviron": ("Klinik Mediviron", "health", "clinic"),
    "airasia": ("AirAsia", "travel", "airline"),
    "malaysia airlines": ("Malaysia Airlines", "travel", "airline"),
    "agoda": ("Agoda", "travel", "hotel"),
    "klook": ("Klook", "travel", "activities"),
    "great eastern": ("Great Eastern", "insurance", "life_insurance"),
    "prudential": ("Prudential", "insurance", "life_insurance"),
    "etiqa": ("Etiqa", "insurance", "general_insurance"),
    "stashaway": ("StashAway", "investment", "robo_advisor"),
    "genting casino": ("Genting Casino", "gambling", "casino"),
    "fitness first": ("Fitness First", "health", "gym"),
    "celebrity fitness": ("Celebrity Fitness", "health", "gym"),
}

# ---- transfer_submethod → channel bucket (derived; brief §2 enrichment v4) ----
CHANNEL_BY_SUBMETHOD: dict[str, str] = {
    "duitnow_qr": "qr",
    "jompay": "bill", "direct_debit": "bill", "auto_debit": "bill",
    "shared_atm_network": "cash", "mydebit_cash_out": "cash", "dnqr_cash_out": "cash",
    "duitnow_transfer": "transfer", "intrabank": "transfer", "ibg": "transfer",
    "rtgs": "transfer", "shared_atm_network_ibft": "transfer",
    "fpx": "online", "debit_card": "card", "debit_card_not_present": "online",
}

# categories that are "wants" rather than "needs" — a PFM/budgeting signal
DISCRETIONARY_CATEGORIES = {
    "food_and_beverage", "shopping", "entertainment", "travel", "gambling",
}


def normalize_merchant(merchant_name: str | None) -> str | None:
    """Lowercase, strip punctuation/outlet suffixes — exact dictionary key form."""
    if not merchant_name:
        return None
    key = "".join(c for c in merchant_name.lower() if c.isalnum() or c == " ").strip()
    # outlet suffix tails ("tealive ss15 subang" -> "tealive") via longest-prefix match
    while key and key not in MERCHANT_DICT:
        parts = key.rsplit(" ", 1)
        if len(parts) == 1:
            break
        key = parts[0]
    return key if key in MERCHANT_DICT else None


def _to_myt(v) -> datetime | None:
    """Parse a wire transaction_date (str ISO or datetime) to a MYT-aware dt."""
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYT)


def _amount_value(txn: dict) -> float:
    """Amount as float — works for wire string amounts and Decimal128 (seed)."""
    try:
        return float(str(txn["amount"]["amount"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _amount_bucket(value: float) -> str:
    if value < 20:
        return "micro"
    if value < 100:
        return "small"
    if value < 500:
        return "medium"
    return "large"


def _channel(txn: dict) -> str | None:
    """Derived payment channel bucket (qr/card/transfer/bill/online/cash)."""
    submethod = txn.get("transfer_submethod")
    if submethod and submethod in CHANNEL_BY_SUBMETHOD:
        return CHANNEL_BY_SUBMETHOD[submethod]
    if txn.get("mcc"):
        return "card"
    method = txn.get("transfer_method")
    if method == "online_payment" or "FPX" in (txn.get("description") or "").upper():
        return "online"
    if method == "instore_payment":
        return "card"
    if method == "cash_withdrawal":
        return "cash"
    return None


def _tags(txn: dict, category: str) -> list[str]:
    tags: list[str] = []
    fx = txn.get("foreign_currency_amount")
    if fx:
        tags.append("fx")
        if (fx.get("currency") or "MYR") != "MYR":
            tags.append("cross_border")
    if txn.get("transfer_method") == "recurring_payment":
        tags.append("subscription")
    if category == "gambling":
        tags.append("gambling")
    if not txn.get("is_settled", True):
        tags.append("unsettled")
    return tags


def categorize(txn: dict) -> dict:
    """Compute the enrichment block for a spec Transaction dict (wire shape).

    Resolution order (v4): salary/NPF heuristics → MCC map → merchant dictionary
    → transfer_submethod map → uncategorized. v4 adds decomposable derived
    signals (temporal, channel, amount_bucket, subcategory, is_discretionary,
    tags, enrichment_source) — all write-time, no regex ever reaches a query.
    """
    desc = (txn.get("description") or "").upper()
    submethod = txn.get("transfer_submethod")
    indicator = txn.get("credit_debit_indicator")

    merchant_key = normalize_merchant(txn.get("merchant_name"))
    merchant_display = MERCHANT_DICT[merchant_key][0] if merchant_key else None
    subcategory = MERCHANT_DICT[merchant_key][2] if merchant_key else None

    if indicator == "credit" and ("SALARY" in desc or "GAJI" in desc):
        category, source = "salary_income", "salary_heuristic"
    elif "NPF" in desc:
        category, source = "npf_contribution", "npf_heuristic"
    elif "INSTALLMENT" in desc or "INSTALMENT" in desc or "LOAN PAYMENT" in desc:
        category, source = "loan_repayment", "loan_heuristic"
    elif txn.get("mcc") and txn["mcc"] in MCC_MAP:
        category, source = MCC_MAP[txn["mcc"]], "mcc"
    elif merchant_key:
        category, source = MERCHANT_DICT[merchant_key][1], "merchant_dict"
    elif submethod and submethod in SUBMETHOD_MAP:
        category, source = SUBMETHOD_MAP[submethod], "submethod"
    else:
        category, source = "uncategorized", "uncategorized"

    myt = _to_myt(txn.get("transaction_date"))

    return {
        "category": category,
        "subcategory": subcategory,
        "merchant_normalized": merchant_display,
        "is_recurring": False,            # stamped by the recurring detector pass
        "recurring_group_id": None,
        "is_transfer_own_account": False,  # stamped by ingestion (account-set check)
        "channel": _channel(txn),
        "amount_bucket": _amount_bucket(_amount_value(txn)),
        "is_discretionary": category in DISCRETIONARY_CATEGORIES,
        "tags": _tags(txn, category),
        "enrichment_source": source,
        "day_of_week": myt.strftime("%a").lower() if myt else None,
        "hour_local": myt.hour if myt else None,
        "is_weekend": (myt.weekday() >= 5) if myt else None,
        "month": str(txn["transaction_date"])[:7],
        "categorizer_version": CATEGORIZER_VERSION,
    }
