"""BSON → JSON for API responses: Decimal128 → string, datetime → ISO UTC."""

from datetime import date, datetime, timezone

from bson import Decimal128, ObjectId


def jsonable(v):
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if isinstance(v, Decimal128):
        return f"{v.to_decimal():.2f}"
    if isinstance(v, datetime):
        return v.replace(tzinfo=v.tzinfo or timezone.utc).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, ObjectId):
        return str(v)
    return v
