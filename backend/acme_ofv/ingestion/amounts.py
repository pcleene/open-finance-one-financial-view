"""Money + date wire<->storage helpers shared across ingestion."""

from datetime import datetime, timezone
from decimal import Decimal

from bson import Decimal128


def d128(s) -> Decimal128:
    return Decimal128(Decimal(str(s)).quantize(Decimal("0.01")))


def amount_to_decimal(a: dict | None) -> dict | None:
    """Wire Amount/BalanceAmount (string amount) → Decimal128 storage form."""
    if not a:
        return None
    out = dict(a)
    out["amount"] = d128(a["amount"])
    return out


def parse_dt(v) -> datetime:
    return datetime.fromisoformat(v).astimezone(timezone.utc)


def mask_display(n: str) -> str:
    return (n[:4] + "****" + n[-4:]) if len(n) >= 8 else n
