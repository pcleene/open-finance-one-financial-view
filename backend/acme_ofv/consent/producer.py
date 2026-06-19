"""Consent-event producer — full post-image per state change, monotonic _rcp_version.

Transport `direct` (POC default): applies exactly the write the MongoDB Kafka
sink connector would (ReplaceOneBusinessKey upsert keyed on consent_id), with
the _rcp_version guard that protects against out-of-order replay — so every
consumer downstream (change stream → profile-updater/eraser) behaves
identically when the topic moves to MSK + Kafka Connect (transport `kafka`,
ops/kafka/ holds the connector config).

The front end / webhook receiver / consent centre NEVER write consent state
into customer_profiles directly: everything rides this one ordered path —
single ordering authority per consent_id (brief §7).
"""

from datetime import datetime, timezone

from acme_ofv.config import settings
from acme_ofv.consent.purpose_map import internal_purposes_for


def _parse_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    return datetime.fromisoformat(v).astimezone(timezone.utc)


def post_image_to_doc(consent: dict) -> dict:
    """Wire-shape Account Access Consent Object → acme_ofv.consents document."""
    doc = dict(consent)
    doc["_id"] = doc["consent_id"]
    doc["schema_version"] = 2
    doc["expiration_datetime"] = _parse_dt(doc["expiration_datetime"])
    doc["created_at"] = _parse_dt(doc["created_at"])
    if doc.get("updated_at"):
        doc["updated_at"] = _parse_dt(doc["updated_at"])
    doc["internal_purposes"] = internal_purposes_for(doc["consent_purpose"])
    # monotonic per consent: ms timestamp of the state change (outbox-assigned in prod)
    basis = doc.get("updated_at") or doc["created_at"]
    doc["_rcp_version"] = int(basis.timestamp() * 1000)
    doc["_cdc"] = {
        "topic": settings().kafka_consent_topic,
        "transport": settings().consent_event_transport,
        "synced_at": datetime.now(timezone.utc),
    }
    return doc


async def publish_consent_event(db, consent_post_image: dict) -> dict:
    """Single ordered path for every consent state change.

    transport=direct (default): apply the sink-equivalent guarded upsert into
    acme_ofv.consents in-process.
    transport=kafka: produce the post-image to rcp.consent.events keyed by
    consent_id (per-consent ordering) and let the MongoDB Kafka Connect sink
    apply the upsert — DO NOT write Mongo here. Nothing downstream of the topic
    changes (brief §7)."""
    doc = post_image_to_doc(consent_post_image)

    # resolve customer_id via the the Open Finance platform identity join key (same for both paths)
    if doc.get("hashed_id_number"):
        profile = await db.customer_profiles.find_one(
            {"customer.hashed_id_number": doc["hashed_id_number"]}, {"_id": 1})
        if profile:
            doc["customer_id"] = profile["_id"]

    if settings().consent_event_transport == "kafka":
        from acme_ofv.streaming.msk_client import get_consent_producer
        await get_consent_producer().publish(
            settings().kafka_consent_topic, value=doc, key=doc["_id"])
        return doc  # the Connect sink performs the upsert into acme_ofv.consents

    # --- direct transport (zero-infra default) ---
    result = await db.consents.replace_one(
        {"_id": doc["_id"], "$or": [
            {"_rcp_version": {"$lt": doc["_rcp_version"]}},
            {"_rcp_version": {"$exists": False}},
        ]},
        doc,
    )
    if result.matched_count == 0:
        try:
            await db.consents.insert_one(doc)
        except Exception:
            pass  # older replay against a newer document — idempotent no-op
    return doc
