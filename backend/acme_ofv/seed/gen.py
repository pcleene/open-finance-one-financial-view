"""Synthetic Malaysia Open Finance data generator (brief §6.1).

Hierarchical: persona → accounts → per-account transaction stream, so
customer_id / hashed_id_number / account context are stamped at generation
time — never joined post-hoc. Amounts are Decimal128 from birth.
"""

import base64
import hashlib
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from bson import Decimal128

from acme_ofv.ingestion.enrichment import MERCHANT_DICT

MYT = timezone(timedelta(hours=8))

PROVIDERS = [
    {
        "provider_id": "DP-BANKB-001-7F3A", "name": "Bank Beta Berhad",
        "status": "online", "provider_type": "bank",
        "supported_use_cases": ["pfm", "credit_underwriting"],
        "display": {"short_name": "Bank Beta", "brand_color": "#FFC600", "logo_key": "bankb"},
        "custom_dialect": {"channel": "MAE"},
    },
    {
        "provider_id": "DP-BANKC-001-9C2B", "name": "Bank Gamma Berhad",
        "status": "online", "provider_type": "bank",
        "supported_use_cases": ["pfm", "credit_underwriting"],
        "display": {"short_name": "Bank Gamma", "brand_color": "#0067B1", "logo_key": "bankc"},
        "custom_dialect": {"src_channel": "BankGamma-Now"},
    },
    {
        "provider_id": "DP-BANKD-001-4E1D", "name": "Bank Delta Berhad",
        "status": "online", "provider_type": "bank",
        "supported_use_cases": ["pfm", "credit_underwriting"],
        "display": {"short_name": "Bank Delta", "brand_color": "#E31837", "logo_key": "bankd"},
        "custom_dialect": {"chnl": "PBe"},
    },
    {
        "provider_id": "DP-BANKE-001-8A5C", "name": "Bank Epsilon Berhad",
        "status": "online", "provider_type": "bank",
        "supported_use_cases": ["pfm", "credit_underwriting"],
        "display": {"short_name": "Bank Epsilon", "brand_color": "#00329A", "logo_key": "banke"},
        "custom_dialect": {"channel": "Bank Epsilon Connect"},
    },
    {
        "provider_id": "DP-NPF-001-2F9E", "name": "National Provident Fund",
        "status": "online", "provider_type": "pension_fund",
        "supported_use_cases": ["pfm"],
        "display": {"short_name": "NPF", "brand_color": "#005AAB", "logo_key": "npf"},
        "custom_dialect": {"scheme": "Akaun Persaraan"},
    },
]
PROVIDER_BY_ID = {p["provider_id"]: p for p in PROVIDERS}
Acme_INTERNAL = "Acme-INTERNAL"

FIRST_M = ["Ahmad", "Farid", "Hafiz", "Amir", "Zulkifli", "Wei Sheng", "Jian Hao",
           "Kumar", "Rajesh", "Daniel", "Adam", "Iskandar", "Syafiq", "Harith"]
FIRST_F = ["Aisyah", "Nurul", "Siti", "Mei Ling", "Li Wen", "Priya", "Kavitha",
           "Sarah", "Aina", "Farah", "Hannah", "Zara", "Alia", "Damia"]
LAST = ["bin Abdullah", "bin Osman", "binti Rahman", "binti Ismail", "Tan", "Lim",
        "Wong", "Lee", "Ng", "a/l Subramaniam", "a/p Devi", "bin Hassan", "binti Yusof"]
EMPLOYERS = ["TechVista Sdn Bhd", "Petronas Dagangan", "Sunway Group", "AirAsia Berhad",
             "Top Glove", "Genting Malaysia", "Axiata", "IHH Healthcare", "Mr DIY Group",
             "Gamuda Berhad", "Telekom Malaysia", "Nestle Malaysia"]

QR_MERCHANTS = ["TEALIVE SS15", "ZUS COFFEE KLCC", "MAMAK MAJU USJ", "KFC WANGSA MAJU",
                "MCDONALDS PJ", "OLD TOWN WHITE COFFEE MID VALLEY", "99 SPEEDMART SEKSYEN 7"]
GROCERY = [("LOTUSS KEPONG", "5411"), ("JAYA GROCER INTERMARK", "5411"),
           ("AEON TAMAN MALURI", "5411"), ("VILLAGE GROCER BANGSAR", "5411"),
           ("99 SPEEDMART SEKSYEN 7", "5499")]
CARD_SPENDS = [  # (merchant, mcc, lo, hi, weight)
    ("SHOPEE MALAYSIA", "5999", 20, 350, 5), ("LAZADA", "5999", 20, 300, 4),
    ("GRAB", "4121", 8, 45, 6), ("GRABFOOD", "5814", 15, 60, 6),
    ("FOODPANDA", "5814", 15, 55, 4), ("PETRONAS NKVE", "5541", 40, 120, 4),
    ("SHELL DAMANSARA", "5541", 40, 110, 3), ("UNIQLO PAVILION", "5651", 60, 400, 2),
    ("ZARA KLCC", "5651", 100, 600, 1), ("GSC MID VALLEY", "7832", 18, 80, 2),
    ("NETFLIX", "7841", 45, 45, 1), ("WATSONS", "5912", 15, 120, 3),
    ("GUARDIAN", "5912", 15, 100, 2), ("MR DIY", "5311", 10, 150, 3),
    ("AGODA", "7011", 200, 900, 1), ("AIRASIA", "4511", 150, 700, 1),
    ("KLINIK MEDIVIRON UOA", "8011", 50, 180, 1), ("STEAM PURCHASE", "5816", 20, 250, 2),
]
BILLS = [("TNB", "TNB BILL PAYMENT", 80, 350), ("AIR SELANGOR", "AIR SELANGOR BILL", 12, 60),
         ("UNIFI", "UNIFI BILL PAYMENT", 129, 129), ("MAXIS", "MAXIS POSTPAID BILL", 75, 160)]
SUBSCRIPTIONS = [("NETFLIX", 54.90), ("SPOTIFY", 23.90), ("DISNEY PLUS", 29.90),
                 ("FITNESS FIRST", 180.00), ("CELEBRITY FITNESS", 159.00)]


def d128(x: float | str | Decimal) -> Decimal128:
    return Decimal128(Decimal(str(x)).quantize(Decimal("0.01")))


def hashed_id(nric: str) -> str:
    normalized = nric.replace("-", "").replace(" ", "").upper()
    return base64.urlsafe_b64encode(hashlib.sha256(normalized.encode()).digest()).decode().rstrip("=")


def acct_no(rng: random.Random, kind: str) -> str:
    if kind == "credit":
        return "4556" + "".join(rng.choices("0123456789", k=12))
    return "".join(rng.choices("0123456789", k=12))


def mask(n: str) -> str:
    return n[:4] + "****" + n[-4:]


class Customer:
    def __init__(self, idx: int, rng: random.Random, persona: dict | None = None):
        self.rng = rng
        self.customer_id = f"acme_cust_{idx:06d}"
        p = persona or {}
        female = p.get("female", rng.random() < 0.5)
        self.full_name = p.get("full_name") or \
            f"{rng.choice(FIRST_F if female else FIRST_M)} {rng.choice(LAST)}"
        yy = rng.randint(70, 99)
        self.nric = p.get("nric") or \
            f"{yy:02d}{rng.randint(1,12):02d}{rng.randint(1,28):02d}-{rng.randint(10,14)}-{rng.randint(1000,9999)}"
        self.hashed_id_number = hashed_id(self.nric)
        self.salary = p.get("salary", float(rng.choice([3500, 4200, 5200, 6500, 7400, 9800, 12500]))
                            * rng.uniform(0.95, 1.05))
        self.payday = p.get("payday", rng.choice([25, 25, 26, 27, 28]))
        self.employer = p.get("employer", rng.choice(EMPLOYERS))
        self.segment = p.get("segment", rng.choice(["mass", "mass", "preferred", "private"]))
        self.gambler = p.get("gambler", rng.random() < 0.06)
        self.has_car = rng.random() < 0.6
        self.email = p.get("email") or f"{self.full_name.split()[0].lower()}***@gmail.com"
        self.accounts: list[dict] = []   # mock-side account docs (all DPs incl Acme)

    def user_doc(self) -> dict:
        return {
            "hashed_id_number": self.hashed_id_number,
            "id_type": "nric",
            "full_name": self.full_name,
            "email_masked": self.email,
            "customer_ref": self.customer_id,  # generator-side linkage convenience
        }


# ----------------------------------------------------------------- accounts

def make_account(c: Customer, dp_id: str, typ: str, subtype: str,
                 is_salary: bool = False) -> dict:
    rng = c.rng
    inst = ("Acme Bank Berhad" if dp_id == Acme_INTERNAL
            else PROVIDER_BY_ID[dp_id]["name"])
    number = acct_no(rng, typ)
    acc = {
        "account_id": str(uuid.uuid4()),
        "dp_id": dp_id,
        "hashed_id_number": c.hashed_id_number,
        "customer_ref": c.customer_id,
        "account_number": number,
        "account_number_display": mask(number),
        "account_name": {
            ("deposit", "savings"): f"{inst.split()[0]} Savings",
            ("deposit", "current"): "Premier Current",
            ("deposit", "pension"): "Akaun Persaraan",
            ("credit", "credit_card"): f"{inst.split()[0]} Visa Platinum",
            ("loan", "hire_purchase"): "Hire Purchase-i",
            ("loan", "mortgage"): "Home Financing",
            ("investment", "fund"): "Unit Trust Fund",
        }.get((typ, subtype), "Account"),
        "account_holder_name": c.full_name.upper(),
        "institution_name": inst,
        "category": "retail",
        "type": typ,
        "subtype": subtype,
        "loan_details": None,
        "limit": None,
        "interest_rate": None,
        "minimum_payment_amount": None,
        "payment_due_date": None,
        "custom_data": (None if dp_id == Acme_INTERNAL
                        else dict(PROVIDER_BY_ID[dp_id]["custom_dialect"])),
        "is_salary": is_salary,
    }

    today = datetime.now(MYT).date()
    if typ == "deposit" and subtype in ("savings", "current"):
        bal = round(rng.uniform(1500, 45000) + (15000 if c.segment == "private" else 0), 2)
        acc["balance_state"] = {
            "current_balance": {"amount": d128(bal), "currency": "MYR",
                                "credit_debit_indicator": "credit"},
            "available_balance": {"amount": d128(bal), "currency": "MYR",
                                  "credit_debit_indicator": "credit"},
            "credit_lines_included": False,
        }
    elif typ == "deposit" and subtype == "pension":
        bal = round(rng.uniform(18000, 320000), 2)
        acc["balance_state"] = {
            "current_balance": {"amount": d128(bal), "currency": "MYR",
                                "credit_debit_indicator": "credit"},
            "available_balance": {"amount": d128(bal), "currency": "MYR",
                                  "credit_debit_indicator": "credit"},
            "credit_lines_included": False,
            "custom_data": {"akaun_persaraan": f"{bal * 0.75:.2f}",
                            "akaun_sejahtera": f"{bal * 0.25:.2f}"},
        }
    elif typ == "credit":
        limit = float(rng.choice([5000, 8000, 12000, 16000, 20000, 30000]))
        owed = round(limit * rng.uniform(0.08, 0.72), 2)
        acc["limit"] = {"amount": d128(limit), "currency": "MYR"}
        acc["interest_rate"] = round(rng.uniform(15.0, 18.0), 2)
        acc["minimum_payment_amount"] = {"amount": d128(max(25.0, owed * 0.05)), "currency": "MYR"}
        acc["payment_due_date"] = (today + timedelta(days=rng.randint(5, 24))).isoformat()
        acc["balance_state"] = {
            "current_balance": {"amount": d128(owed), "currency": "MYR",
                                "credit_debit_indicator": "debit"},
            "available_balance": {"amount": d128(limit - owed), "currency": "MYR",
                                  "credit_debit_indicator": "credit"},
            "statement_balance": {"amount": d128(owed * rng.uniform(0.7, 1.0)),
                                  "currency": "MYR", "credit_debit_indicator": "debit"},
            "statement_date": (today - timedelta(days=rng.randint(1, 20))).isoformat(),
            "credit_lines_included": True,
        }
    elif typ == "loan":
        principal = float(rng.choice([48000, 68000, 90000])) if subtype == "hire_purchase" \
            else float(rng.choice([380000, 520000, 740000]))
        orig = today - timedelta(days=rng.randint(400, 2600))
        years = 7 if subtype == "hire_purchase" else 30
        installment = round(principal / (years * 12) * rng.uniform(1.15, 1.35), 2)
        outstanding = round(principal * rng.uniform(0.35, 0.85), 2)
        acc["loan_details"] = {
            "loan_amount": {"amount": d128(principal), "currency": "MYR"},
            "origination_date": orig.isoformat(),
            "maturity_date": (orig + timedelta(days=365 * years)).isoformat(),
        }
        acc["interest_rate"] = round(rng.uniform(2.3, 4.6), 2)
        acc["minimum_payment_amount"] = {"amount": d128(installment), "currency": "MYR"}
        acc["payment_due_date"] = (today.replace(day=1) + timedelta(days=31 + rng.randint(0, 9))).isoformat()
        acc["balance_state"] = {
            "current_balance": {"amount": d128(outstanding), "currency": "MYR",
                                "credit_debit_indicator": "debit"},
            "credit_lines_included": False,
        }
    elif typ == "investment":
        bal = round(rng.uniform(8000, 160000), 2)
        acc["balance_state"] = {
            "current_balance": {"amount": d128(bal), "currency": "MYR",
                                "credit_debit_indicator": "credit"},
            "credit_lines_included": False,
        }
    c.accounts.append(acc)
    return acc


# ------------------------------------------------------------- transactions

def _t(acc: dict, when: datetime, indicator: str, amount: float, description: str,
       method: str | None = None, submethod: str | None = None,
       merchant: str | None = None, mcc: str | None = None,
       fx: tuple[float, str] | None = None, settled: bool = True,
       recipient_ref: str | None = None) -> dict:
    txn = {
        "account_id": acc["account_id"],
        "dp_id": acc["dp_id"],
        "transaction_id": f"txn-{when:%Y%m%d}-{uuid.uuid4().hex[:12]}",
        "transaction_date": when.astimezone(timezone.utc),
        "credit_debit_indicator": indicator,
        "amount": {"amount": d128(amount), "currency": "MYR"},
        "foreign_currency_amount": (
            {"amount": d128(fx[0]), "currency": fx[1]} if fx else None),
        "transfer_method": method if acc["type"] == "deposit" else None,
        "transfer_submethod": submethod if acc["type"] == "deposit" else None,
        "description": description[:300],
        "recipient_reference": recipient_ref,
        "other_payment_description": None,
        "merchant_name": merchant[:100] if merchant else None,
        "mcc": mcc if acc["subtype"] == "credit_card" else None,
        "is_settled": settled,
        "custom_data": acc.get("custom_data"),
    }
    return txn


def gen_transactions(c: Customer, acc: dict, months: int = 6) -> list[dict]:
    rng = c.rng
    out: list[dict] = []
    now = datetime.now(MYT)
    start = now - timedelta(days=months * 30)
    typ, sub = acc["type"], acc["subtype"]

    def rand_time(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, rng.randint(7, 22),
                        rng.randint(0, 59), rng.randint(0, 59), tzinfo=MYT)

    # month iterator over the window
    months_list = []
    cur = start.date().replace(day=1)
    while cur <= now.date():
        months_list.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)

    if typ == "deposit" and sub in ("savings", "current"):
        my_subs = rng.sample(SUBSCRIPTIONS, k=rng.randint(1, 3)) if acc["is_salary"] else []
        my_bills = rng.sample(BILLS, k=rng.randint(2, 4)) if acc["is_salary"] else []
        for m0 in months_list:
            if acc["is_salary"]:
                pay_d = date(m0.year, m0.month, min(c.payday, 28))
                if start.date() <= pay_d <= now.date():
                    out.append(_t(acc, rand_time(pay_d), "credit",
                                  round(c.salary * rng.uniform(0.995, 1.005), 2),
                                  f"SALARY CREDIT - {c.employer.upper()}",
                                  "funds_transfer", rng.choice(["intrabank", "ibg"])))
                for name, amt in my_subs:
                    d_ = date(m0.year, m0.month, min(rng.randint(1, 12), 28))
                    if start.date() <= d_ <= now.date():
                        out.append(_t(acc, rand_time(d_), "debit", amt,
                                      f"DIRECT DEBIT - {name}",
                                      "recurring_payment", "direct_debit", merchant=name))
                for biller, desc, lo, hi in my_bills:
                    d_ = date(m0.year, m0.month, min(rng.randint(3, 20), 28))
                    if start.date() <= d_ <= now.date():
                        out.append(_t(acc, rand_time(d_), "debit",
                                      round(rng.uniform(lo, hi), 2), desc,
                                      "bill_payment", "jompay", merchant=biller,
                                      recipient_ref=f"JOMPAY{rng.randint(10000000, 99999999)}"))
        # day-level streams across the whole window
        n_days = (now.date() - start.date()).days
        for _ in range(int(n_days / 30 * rng.randint(8, 16))):  # DuitNow QR
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            merchant = rng.choice(QR_MERCHANTS)
            out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(5, 48), 2),
                          f"DUITNOW QR PAYMENT - {merchant}",
                          "instore_payment", "duitnow_qr", merchant=merchant,
                          recipient_ref=f"DN{rng.randint(10**16, 10**17-1)}"))
        for _ in range(int(n_days / 7)):  # groceries
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            merchant, _mcc = rng.choice(GROCERY)
            out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(38, 240), 2),
                          f"DEBIT CARD PURCHASE - {merchant}",
                          "instore_payment", "debit_card", merchant=merchant))
        for _ in range(int(n_days / 30 * rng.randint(2, 5))):  # FPX online
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            merchant = rng.choice(["SHOPEE MALAYSIA", "LAZADA"])
            out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(18, 320), 2),
                          f"FPX PAYMENT - {merchant}", "online_payment", "fpx",
                          merchant=merchant))
        for _ in range(int(n_days / 30 * rng.randint(1, 2))):  # ATM
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            out.append(_t(acc, rand_time(d_), "debit", float(rng.choice([100, 200, 300, 500])),
                          "ATM CASH WITHDRAWAL", "cash_withdrawal", "shared_atm_network"))
        if c.has_car and acc["is_salary"]:
            for _ in range(int(n_days / 9)):
                d_ = start.date() + timedelta(days=rng.randint(0, n_days))
                merchant = rng.choice(["PETRONAS NKVE", "SHELL DAMANSARA", "PETRON SUBANG"])
                out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(40, 110), 2),
                              f"DEBIT CARD PURCHASE - {merchant}",
                              "instore_payment", "debit_card", merchant=merchant))
        for _ in range(rng.randint(0, 3)):  # FX rows
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            usd = round(rng.uniform(12, 120), 2)
            out.append(_t(acc, rand_time(d_), "debit", round(usd * 4.18, 2),
                          "CARD NOT PRESENT - AGODA SINGAPORE",
                          "online_payment", "debit_card_not_present",
                          merchant="AGODA", fx=(usd, rng.choice(["USD", "SGD"]))))
        for _ in range(int(n_days / 30 * rng.randint(1, 3))):  # incoming transfers
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            out.append(_t(acc, rand_time(d_), "credit", round(rng.uniform(20, 260), 2),
                          "DUITNOW TRANSFER RECEIVED",
                          "funds_transfer", "duitnow_transfer",
                          recipient_ref=f"DN{rng.randint(10**16, 10**17-1)}"))
        if len([a for a in c.accounts if a["type"] == "deposit"]) > 1:
            for m0 in months_list:  # own-account moves (de-dup demo)
                d_ = date(m0.year, m0.month, min(rng.randint(1, 27), 28))
                if start.date() <= d_ <= now.date():
                    out.append(_t(acc, rand_time(d_), "debit",
                                  float(rng.choice([300, 500, 1000])),
                                  "TRANSFER TO OWN ACCOUNT",
                                  "funds_transfer", "duitnow_transfer"))

    elif sub == "credit_card":
        n_days = (now.date() - start.date()).days
        spends = rng.choices(CARD_SPENDS, weights=[w for *_, w in CARD_SPENDS],
                             k=int(n_days / 30 * rng.randint(10, 22)))
        for merchant, mcc, lo, hi, _w in spends:
            d_ = start.date() + timedelta(days=rng.randint(0, n_days))
            out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(lo, hi), 2),
                          f"{merchant} {'KUALA LUMPUR' if rng.random() < .5 else 'MY'}",
                          merchant=merchant, mcc=mcc,
                          settled=(now.date() - d_).days > 2 or rng.random() > 0.6))
        if c.gambler:
            for _ in range(rng.randint(3, 8)):
                d_ = start.date() + timedelta(days=rng.randint(0, n_days))
                out.append(_t(acc, rand_time(d_), "debit", round(rng.uniform(200, 1500), 2),
                              "GENTING CASINO RESORT", merchant="GENTING CASINO", mcc="7995"))
        for m0 in months_list:  # statement payment
            d_ = date(m0.year, m0.month, min(rng.randint(14, 26), 28))
            if start.date() <= d_ <= now.date():
                out.append(_t(acc, rand_time(d_), "credit",
                              round(rng.uniform(400, 2600), 2),
                              "PAYMENT RECEIVED - THANK YOU"))

    elif typ == "loan":
        inst_amt = float(acc["minimum_payment_amount"]["amount"].to_decimal())
        for i, m0 in enumerate(months_list):
            d_ = date(m0.year, m0.month, min(rng.randint(5, 9), 28))
            if start.date() <= d_ <= now.date():
                out.append(_t(acc, rand_time(d_), "debit", inst_amt,
                              "HP INSTALLMENT" if sub == "hire_purchase" else "MORTGAGE INSTALMENT"))

    elif typ == "investment":
        for m0 in months_list:
            d_ = date(m0.year, m0.month, min(rng.randint(1, 5), 28))
            if start.date() <= d_ <= now.date():
                out.append(_t(acc, rand_time(d_), "debit", float(rng.choice([200, 500, 1000])),
                              "FUND SUBSCRIPTION - MONTHLY PLAN"))
        out.append(_t(acc, rand_time(now.date() - timedelta(days=rng.randint(10, 80))),
                      "credit", round(rng.uniform(80, 900), 2), "FUND DISTRIBUTION"))

    elif sub == "pension":
        contribution = round(c.salary * 0.23, 2)  # 11% employee + 12% employer
        for m0 in months_list:
            d_ = date(m0.year, m0.month, min(14, 28))
            if start.date() <= d_ <= now.date():
                out.append(_t(acc, rand_time(d_), "credit", contribution,
                              "NPF CONTRIBUTION - EMPLOYEE AND EMPLOYER"))

    return out


# ------------------------------------------------------------ account plans

def plan_external_accounts(c: Customer, dp_ids: list[str]) -> None:
    """Create mock-side accounts at each external DP for this customer."""
    rng = c.rng
    for dp_id in dp_ids:
        if dp_id == "DP-NPF-001-2F9E":
            make_account(c, dp_id, "deposit", "pension")
            continue
        # first bank in the list carries the salary account
        is_salary_dp = dp_id == dp_ids[0]
        make_account(c, dp_id, "deposit", rng.choice(["savings", "savings", "current"]),
                     is_salary=is_salary_dp)
        if rng.random() < 0.55:
            make_account(c, dp_id, "credit", "credit_card")
        if rng.random() < 0.25:
            make_account(c, dp_id, "loan", rng.choice(["hire_purchase", "mortgage"]))
        if rng.random() < 0.18:
            make_account(c, dp_id, "investment", "fund")


def plan_internal_accounts(c: Customer) -> None:
    rng = c.rng
    make_account(c, Acme_INTERNAL, "deposit", "savings",
                 is_salary=False)  # spending account; salary may sit externally
    if rng.random() < 0.5:
        make_account(c, Acme_INTERNAL, "credit", "credit_card")
