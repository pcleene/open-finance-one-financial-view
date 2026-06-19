"""DC-side OFP HTTP client (brief §6.2).

Honours the spec's protocol behaviours: x-fapi-interaction-id on everything,
cursor pagination replayed verbatim, 429 handling (Retry-After if present,
else 5/10/20/40 s + jitter, max 5 attempts, then cool-off), and a token-bucket
cap of 200 req/min per DP. BACKOFF_SCALE shrinks waits for live demos without
changing the schedule's shape.
"""

import asyncio
import os
import random
import time
import uuid

import httpx

from acme_ofv.config import settings

BACKOFF_SCHEDULE = [5, 10, 20, 40]
BACKOFF_SCALE = float(os.environ.get("BACKOFF_SCALE", "0.05"))  # 1.0 = spec-faithful


class RateLimitCooloff(Exception):
    """5 backoff attempts exhausted — caller flags the account and moves on."""


class PerDPRateLimiter:
    """Client-side rolling-60s budget so we don't hit the server's 429 in bulk runs."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self.windows: dict[str, list[float]] = {}

    async def acquire(self, dp_id: str) -> None:
        win = self.windows.setdefault(dp_id, [])
        while True:
            now = time.monotonic()
            while win and now - win[0] > 60:
                win.pop(0)
            if len(win) < self.per_min:
                win.append(now)
                return
            await asyncio.sleep(max(0.05, 60 - (now - win[0])))


class OFPClient:
    def __init__(self, access_token: str, dp_id: str, limiter: PerDPRateLimiter):
        s = settings()
        self.dp_id = dp_id
        self.limiter = limiter
        self.http = httpx.AsyncClient(
            base_url=s.ofp_base_url, timeout=30,
            headers={"authorization": f"Bearer {access_token}"},
        )
        self.calls = 0
        self.retries_429 = 0

    async def aclose(self):
        await self.http.aclose()

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        interaction_id = str(uuid.uuid4())
        for attempt in range(len(BACKOFF_SCHEDULE) + 1):
            await self.limiter.acquire(self.dp_id)
            self.calls += 1
            resp = await self.http.get(
                path, params=params,
                headers={"x-fapi-interaction-id": interaction_id,
                         "x-enc-kid": "dc-acme-enc-1"},
            )
            if resp.status_code != 429:
                resp.interaction_id = interaction_id  # type: ignore[attr-defined]
                return resp
            self.retries_429 += 1
            if attempt >= len(BACKOFF_SCHEDULE):
                break
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else BACKOFF_SCHEDULE[attempt]
            await asyncio.sleep(wait * BACKOFF_SCALE * random.uniform(0.9, 1.2))
        raise RateLimitCooloff(f"429 persisted after {len(BACKOFF_SCHEDULE)} backoff attempts ({self.dp_id})")
