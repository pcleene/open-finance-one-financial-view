"""Per-request MongoDB query capture for the Query Inspector.

Uses a contextvar to hold the list of operations executed during one request;
the API middleware resets it per request and injects the captured queries into
the JSON response as `_queries`. Kept dependency-free (bson + stdlib only) so
the low-level db layer can import it without cycles.

Values are made JSON-safe (Decimal128/datetime/ObjectId), long float arrays
(query vectors) are truncated to a preview, and result documents are trimmed.
"""

import time
from contextvars import ContextVar
from datetime import date, datetime

from bson import Decimal128, ObjectId

_queries: ContextVar[list | None] = ContextVar("_queries", default=None)


def reset_log() -> None:
    _queries.set([])


def get_log() -> list:
    return _queries.get() or []


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, Decimal128):
        return str(obj.to_decimal())
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    return obj


def _truncate_embeddings(obj, max_items: int = 3):
    """Replace long float arrays (query vectors) with a truncated preview."""
    if isinstance(obj, dict):
        return {k: _truncate_embeddings(v, max_items) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > max_items and all(isinstance(x, (int, float)) for x in obj[:max_items + 1]):
            head = [round(x, 4) if isinstance(x, float) else x for x in obj[:max_items]]
            return head + [f"...{len(obj)} dims"]
        return [_truncate_embeddings(x, max_items) for x in obj]
    return obj


def _truncate_result(obj, max_keys: int = 60, max_array: int = 2, depth: int = 0):
    """Compact preview of a result document — keeps the shape, trims big arrays."""
    if depth > 6:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_keys:
                out[f"...+{len(obj) - max_keys} keys"] = "..."
                break
            out[k] = _truncate_result(v, max_keys, max_array, depth + 1)
        return out
    if isinstance(obj, list):
        if len(obj) <= max_array:
            return [_truncate_result(x, max_keys, max_array, depth + 1) for x in obj]
        preview = [_truncate_result(x, max_keys, max_array, depth + 1) for x in obj[:max_array]]
        preview.append(f"...+{len(obj) - max_array} more")
        return preview
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, str) and len(obj) > 160:
        return obj[:160] + "..."
    return obj


def log_query(collection: str, operation: str, args, duration_ms: float = 0, result=None) -> None:
    """Record one MongoDB operation (no-op outside a request context)."""
    log = _queries.get()
    if log is None:
        return
    entry: dict = {
        "collection": collection,
        "operation": operation,
        "args": _truncate_embeddings(_json_safe(args)),
        "duration_ms": round(duration_ms, 1),
        "ts": time.time(),
    }
    if result is not None:
        # true count of docs returned (before the preview is truncated)
        entry["result_count"] = len(result) if isinstance(result, (list, tuple)) else 1
        try:
            entry["result"] = _truncate_result(_json_safe(result))
        except Exception:
            pass
    log.append(entry)
