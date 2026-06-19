"""Scale-ops service (brief §8, §11) — SSE feeds off change streams, the
revocation-storm driver (N concurrent revocations through the full pipeline
while a steady One View read load measures p50/p99), storm status, and
on-demand incremental sync.

Relocated from the former api/app.py ops handlers (behavior-preserving)."""

import asyncio
import json
import random
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sse_starlette.sse import EventSourceResponse

from acme_ofv.api.serialize import jsonable
from acme_ofv.config import settings
from acme_ofv.consent.gate import one_view_pipeline
from acme_ofv.consent.producer import publish_consent_event
from acme_ofv.db import aggregate_list


class OpsService:
    def __init__(self, app):
        self.app = app
        self.db = app.state.db
        self.s = settings()

    async def _client_token(self) -> str:
        if not self.app.state.cc_token:
            async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
                r = await hc.post("/v1/oauth/token",
                                  json={"grant_type": "client_credentials", "client_id": self.s.dc_id})
            self.app.state.cc_token = r.json()["access_token"]
        return self.app.state.cc_token

    def events_response(self) -> EventSourceResponse:
        db = self.db

        async def gen():
            queue: asyncio.Queue = asyncio.Queue()

            async def watch(coll, label):
                try:
                    async with await coll.watch(full_document="updateLookup") as stream:
                        async for ch in stream:
                            await queue.put((label, ch.get("fullDocument") or {}))
                except Exception:
                    pass

            tasks = [asyncio.create_task(watch(db.consent_audit_log, "audit")),
                     asyncio.create_task(watch(db.erasure_jobs, "erasure"))]
            try:
                while True:
                    label, doc = await queue.get()
                    doc.pop("_id", None)
                    yield {"event": label, "data": json.dumps(jsonable(doc))}
            finally:
                for t in tasks:
                    t.cancel()

        return EventSourceResponse(gen())

    async def metrics(self) -> dict:
        db = self.db
        now = datetime.now(timezone.utc)
        jobs = await aggregate_list(db.erasure_jobs, [
            {"$group": {"_id": "$status", "count": {"$sum": 1},
                        "docs_deleted": {"$sum": "$docs_deleted"},
                        "snapshots_deleted": {"$sum": "$snapshots_deleted"}}},
        ])
        recent_audit = await db.consent_audit_log.count_documents(
            {"at": {"$gte": now - timedelta(minutes=10)}})
        consent_states = await aggregate_list(db.consents, [
            {"$group": {"_id": "$status", "n": {"$sum": 1}}}])
        latest_run = await db.ops_runs.find_one(sort=[("started_at", -1)])
        return jsonable({
            "erasure_jobs": {j["_id"]: {"count": j["count"], "docs": j["docs_deleted"],
                                        "snapshots": j["snapshots_deleted"]} for j in jobs},
            "audit_events_10m": recent_audit,
            "consent_states": {c["_id"]: c["n"] for c in consent_states},
            "latest_storm": latest_run,
            "counts": {
                "transactions": await db.transactions.estimated_document_count(),
                "profiles": await db.customer_profiles.estimated_document_count(),
                "consents": await db.consents.estimated_document_count(),
            },
        })

    async def storm(self, n: int = 200, read_rps: int = 50) -> dict:
        db = self.db
        n = min(int(n), 2000)
        read_rps = int(read_rps)
        run_id = f"storm_{uuid.uuid4().hex[:8]}"

        candidates = await db.consents.find(
            {"status": "authorized", "customer_id": {"$gte": "acme_cust_001000"}},
            {"consent_id": 1},
        ).limit(n).to_list(None)
        if not candidates:
            raise HTTPException(400, "no authorized cohort consents left — reseed/relink")

        await db.ops_runs.insert_one({
            "_id": run_id, "started_at": datetime.now(timezone.utc), "kind": "revocation_storm",
            "requested": n, "found": len(candidates), "read_rps": read_rps,
            "status": "running", "revoked": 0, "errors": 0, "error_samples": [],
            "read_p50_ms": None, "read_p99_ms": None,
        })
        asyncio.create_task(self._run_storm(run_id, candidates, read_rps))
        return {"run_id": run_id, "consents_targeted": len(candidates)}

    async def _run_storm(self, run_id: str, candidates: list[dict], read_rps: int) -> None:
        db = self.db
        token = await self._client_token()
        sem = asyncio.Semaphore(40)
        revoked = 0
        errors = 0
        error_samples: list[str] = []  # capped sample of why revokes didn't complete
        storm_done = asyncio.Event()
        latencies: list[float] = []

        persona_ids = [d["_id"] async for d in db.customer_profiles.find({}, {"_id": 1}).limit(50)]

        def note_error(reason: str) -> None:
            nonlocal errors
            errors += 1
            if len(error_samples) < 8:
                error_samples.append(reason)

        async def revoke_one(c):
            nonlocal revoked
            async with sem:
                try:
                    async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=30) as hc:
                        r = await hc.post(f"/v1/consents/{c['consent_id']}/revoke",
                                          json={"updated_by": "data_consumer_user"},
                                          headers={"authorization": f"Bearer {token}"})
                    if r.status_code == 201:
                        await publish_consent_event(db, r.json())
                        revoked += 1
                        if revoked % 25 == 0:
                            await db.ops_runs.update_one({"_id": run_id},
                                                         {"$set": {"revoked": revoked}})
                    else:
                        # surface why (was silently swallowed before) — e.g. the mock
                        # rejects revoke on an already-revoked consent, or token expiry
                        note_error(f"http_{r.status_code}: {r.text[:80]}")
                except Exception as exc:
                    note_error(f"{type(exc).__name__}: {str(exc)[:80]}")

        async def read_load():
            while not storm_done.is_set():
                t0 = time.perf_counter()
                cid = random.choice(persona_ids)
                await aggregate_list(db.customer_profiles, one_view_pipeline(cid, "one_view"))
                latencies.append((time.perf_counter() - t0) * 1000)
                await asyncio.sleep(max(0.0, 1.0 / read_rps - (time.perf_counter() - t0)))

        reader = asyncio.create_task(read_load())
        await asyncio.gather(*(revoke_one(c) for c in candidates))
        await asyncio.sleep(3)
        storm_done.set()
        await reader

        qs = statistics.quantiles(latencies, n=100) if len(latencies) >= 100 else None
        await db.ops_runs.update_one({"_id": run_id}, {"$set": {
            "status": "completed", "revoked": revoked,
            "errors": errors, "error_samples": error_samples,
            "finished_at": datetime.now(timezone.utc),
            "reads_sampled": len(latencies),
            "read_p50_ms": round(statistics.median(latencies), 2) if latencies else None,
            "read_p99_ms": round(qs[98], 2) if qs else None,
            "read_max_ms": round(max(latencies), 2) if latencies else None,
        }})

    async def list_runs(self) -> dict:
        docs = await self.db.ops_runs.find({}).sort("started_at", -1).limit(20).to_list(None)
        return {"runs": jsonable(docs)}

    async def storm_status(self, run_id: str) -> dict:
        db = self.db
        doc = await db.ops_runs.find_one({"_id": run_id})
        if not doc:
            raise HTTPException(404, "unknown run")
        erasure = await aggregate_list(db.erasure_jobs, [
            {"$match": {"created_at": {"$gte": doc["started_at"]}}},
            {"$group": {"_id": "$status", "n": {"$sum": 1},
                        "docs": {"$sum": "$docs_deleted"},
                        "snapshots": {"$sum": "$snapshots_deleted"}}},
        ])
        return jsonable({**doc, "erasure": {e["_id"]: e for e in erasure}})

    async def trigger_sync(self, customer_id: str) -> dict:
        from acme_ofv.ingestion.service import incremental_sync_consent
        db = self.db
        now = datetime.now(timezone.utc)
        consents = await db.consents.find({
            "customer_id": customer_id, "status": "authorized",
            "expiration_datetime": {"$gt": now},
            "permissions": "read_transactions",
        }).to_list(None)
        results = []
        for c in consents:
            results.append(await incremental_sync_consent(db, c))
        return {"synced_consents": len(results), "results": results}
