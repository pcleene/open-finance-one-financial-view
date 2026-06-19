"""MSK consent-event producer — aiokafka with IAM/OAUTHBEARER (in-VPC) or a
plaintext mode for a local docker Kafka (off-VPC dev).

The post-image is published as **Extended JSON** (bson.json_util) keyed by
consent_id. The Kafka Connect MongoDB sink is configured with StringConverter,
so it parses the Extended JSON into a real BSON document ($date/$numberDecimal
become Date/Decimal128) — byte-faithful with the direct-transport upsert, so the
consents change stream + downstream worker behave identically (brief §7).

Pattern mirrors the proven Astro Billing MSK producer.
"""

from __future__ import annotations

import asyncio

from aiokafka import AIOKafkaProducer
from aiokafka.abc import AbstractTokenProvider
from aiokafka.helpers import create_ssl_context
from bson import json_util

from acme_ofv.config import settings


class _MSKTokenProvider(AbstractTokenProvider):
    """aiokafka calls token() on each refresh; the AWS signer returns a
    short-lived IAM auth token (credentials come from the EC2 instance role)."""

    def __init__(self, region: str) -> None:
        self._region = region

    async def token(self) -> str:
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        token, _expiry_ms = MSKAuthTokenProvider.generate_auth_token(self._region)
        return token


def _auth_kwargs() -> dict:
    """SASL_SSL + OAUTHBEARER for MSK IAM; nothing extra for plaintext."""
    s = settings()
    if s.kafka_auth == "plaintext":
        return {}
    return {
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "OAUTHBEARER",
        "sasl_oauth_token_provider": _MSKTokenProvider(s.aws_region),
        "ssl_context": create_ssl_context(),
    }


class ConsentEventProducer:
    """Lazy singleton AIOKafkaProducer for consent post-images."""

    def __init__(self) -> None:
        self._producer: AIOKafkaProducer | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._producer is not None:
            return
        async with self._lock:
            if self._producer is not None:
                return
            producer = AIOKafkaProducer(
                bootstrap_servers=settings().kafka_bootstrap,
                value_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
                key_serializer=lambda k: k.encode("utf-8") if isinstance(k, str) else k,
                acks="all",
                enable_idempotence=True,
                request_timeout_ms=30_000,
                **_auth_kwargs(),
            )
            await producer.start()
            self._producer = producer
            print(f"[consent-producer] started — brokers={settings().kafka_bootstrap} "
                  f"auth={settings().kafka_auth}", flush=True)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, topic: str, value: dict, key: str) -> None:
        if self._producer is None:
            await self.start()
        payload = json_util.dumps(value)  # Extended JSON: dates/decimals survive
        await self._producer.send_and_wait(topic, value=payload, key=key)


_singleton: ConsentEventProducer | None = None


def get_consent_producer() -> ConsentEventProducer:
    global _singleton
    if _singleton is None:
        _singleton = ConsentEventProducer()
    return _singleton
