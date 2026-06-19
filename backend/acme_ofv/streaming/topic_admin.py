"""Idempotent MSK topic creation (same auth path as the producer).

Safe to call on every startup / before a simulation run.
"""

from __future__ import annotations

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from acme_ofv.config import settings
from acme_ofv.streaming.msk_client import _auth_kwargs


async def ensure_consent_topic() -> bool:
    """Create rcp.consent.events (key=consent_id ⇒ per-consent ordering). Returns
    True if created, False if it already existed."""
    s = settings()
    admin = AIOKafkaAdminClient(bootstrap_servers=s.kafka_bootstrap, **_auth_kwargs())
    await admin.start()
    try:
        topic = NewTopic(
            name=s.kafka_consent_topic,
            num_partitions=s.kafka_consent_partitions,
            replication_factor=s.kafka_consent_replication,
            topic_configs={"retention.ms": str(7 * 24 * 3600 * 1000)},
        )
        try:
            await admin.create_topics([topic])
            print(f"[topic-admin] created {s.kafka_consent_topic}", flush=True)
            return True
        except TopicAlreadyExistsError:
            print(f"[topic-admin] exists {s.kafka_consent_topic}", flush=True)
            return False
    finally:
        await admin.close()
