"""Transaction-ingestion simulator (brief §6.2 consumption pattern).

In-app async load driver (same shape as the revocation storm): drives the real
the Open Finance platform consumption path — `incremental_sync_consent` per authorized consent,
through the per-DP 200 req/min token bucket, cursor pagination and 429 backoff —
across a configurable cohort, with live counters persisted to `simulation_runs`
so progress is trackable and results are retrievable afterwards.
"""

import asyncio
import statistics
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import HTTPException

from acme_ofv.api.serialize import jsonable
from acme_ofv.config import settings
from acme_ofv.consent.producer import publish_consent_event

CANDIDATE_PROJECTION = {
    "consent_id": 1, "dp_id": 1, "customer_id": 1, "accounts": 1,
    "hashed_id_number": 1, "consent_purpose": 1, "permissions": 1,
}


class SimulationService:
    """Constructed with the FastAPI app (spawns background tasks; reads app.state.db)."""

    def __init__(self, app):
        self.app = app
        self.db = app.state.db
        self.s = settings()

    async def start(self, count: int = 200, concurrency: int = 8,
                    mode: str = "incremental") -> dict:
        db = self.db
        count = max(1, min(int(count), 2000))
        concurrency = max(1, min(int(concurrency), 32))
        now = datetime.now(timezone.utc)
        if mode == "reauthorize":
            return await self._start_reauthorize(count, concurrency, now)
        if mode == "reseed":
            return await self._start_reseed(count, concurrency, now)
        candidates = await db.consents.find({
            "status": "authorized",
            "permissions": "read_transactions",
            "expiration_datetime": {"$gt": now},
            "customer_id": {"$ne": None},
        }, CANDIDATE_PROJECTION).limit(count).to_list(None)
        if not candidates:
            raise HTTPException(400, "no authorized read_transactions consents — link/seed first")

        run_id = f"sim_{uuid.uuid4().hex[:8]}"
        await db.simulation_runs.insert_one({
            "_id": run_id, "kind": "ingestion_sim", "started_at": now,
            "config": {"count": count, "concurrency": concurrency},
            "found": len(candidates), "status": "running",
            "consents_done": 0, "transactions": 0, "calls": 0, "retries_429": 0, "errors": 0,
            "pull_p50_ms": None, "pull_p99_ms": None, "elapsed_ms": 0, "throughput_tps": None,
        })
        asyncio.create_task(self._run(run_id, candidates, concurrency))
        return {"run_id": run_id, "consents_targeted": len(candidates)}

    async def _run(self, run_id: str, candidates: list[dict], concurrency: int) -> None:
        from acme_ofv.ingestion.service import incremental_sync_consent
        db = self.db
        sem = asyncio.Semaphore(max(1, concurrency))
        started = time.perf_counter()
        latencies: list[float] = []
        totals = {"consents_done": 0, "transactions": 0, "calls": 0, "retries_429": 0, "errors": 0}

        async def one(c):
            async with sem:
                t0 = time.perf_counter()
                try:
                    res = await incremental_sync_consent(db, c)
                except Exception:
                    res = {"error": "exception"}
                latencies.append((time.perf_counter() - t0) * 1000)
                totals["consents_done"] += 1
                totals["transactions"] += int(res.get("transactions", 0) or 0)
                totals["calls"] += int(res.get("calls", 0) or 0)
                totals["retries_429"] += int(res.get("retries_429", 0) or 0)
                if res.get("error"):
                    totals["errors"] += 1
                if totals["consents_done"] % 5 == 0:
                    await self._persist(run_id, totals, latencies, started, "running")

        try:
            await asyncio.gather(*(one(c) for c in candidates))
            await self._persist(run_id, totals, latencies, started, "completed")
        except Exception:
            await self._persist(run_id, totals, latencies, started, "failed")

    async def _persist(self, run_id: str, totals: dict, latencies: list[float],
                       started: float, status: str) -> None:
        elapsed = time.perf_counter() - started
        p50 = round(statistics.median(latencies), 1) if latencies else None
        p99 = (round(statistics.quantiles(latencies, n=100)[98], 1)
               if len(latencies) >= 100 else (round(max(latencies), 1) if latencies else None))
        upd = {
            **totals,
            "pull_p50_ms": p50, "pull_p99_ms": p99,
            "elapsed_ms": round(elapsed * 1000),
            "throughput_tps": round(totals["transactions"] / elapsed, 1) if elapsed > 0 else None,
            "status": status,
        }
        if status in ("completed", "failed"):
            upd["finished_at"] = datetime.now(timezone.utc)
        await self.db.simulation_runs.update_one({"_id": run_id}, {"$set": upd})

    # ----------------------------------------------------------------------
    # reauthorize mode — fire brand-new consents through the real LCM path:
    # revoke -> worker erases the now-uncovered accounts -> re-link (PAR ->
    # authorize -> token) -> worker backfills, reconstructing balances from the
    # freshly pulled transactions. The simulator only orchestrates + measures;
    # the eraser/backfill worker does the real work (must be running).
    # ----------------------------------------------------------------------

    async def _client_token(self) -> str:
        if not getattr(self.app.state, "cc_token", None):
            async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
                r = await hc.post("/v1/oauth/token",
                                  json={"grant_type": "client_credentials",
                                        "client_id": self.s.dc_id})
            self.app.state.cc_token = r.json()["access_token"]
        return self.app.state.cc_token

    async def _start_reauthorize(self, count: int, concurrency: int, now) -> dict:
        db = self.db
        candidates = await db.consents.find({
            "status": "authorized",
            "expiration_datetime": {"$gt": now},
            "customer_id": {"$ne": None},
            "accounts.0": {"$exists": True},
        }, CANDIDATE_PROJECTION).limit(count).to_list(None)
        if not candidates:
            raise HTTPException(400, "no authorized consents to re-authorize — link/seed first")

        run_id = f"reauth_{uuid.uuid4().hex[:8]}"
        await db.simulation_runs.insert_one({
            "_id": run_id, "kind": "reauth_sim", "mode": "reauthorize", "started_at": now,
            "config": {"count": count, "concurrency": concurrency},
            "found": len(candidates), "status": "running",
            "consents_done": 0, "consents_revoked": 0, "consents_relinked": 0,
            "backfills_done": 0, "snapshots_rebuilt": 0, "docs_erased": 0,
            "transactions": 0, "calls": 0, "errors": 0,
            "elapsed_ms": 0, "throughput_tps": None,
        })
        asyncio.create_task(self._run_reauthorize(run_id, candidates, concurrency))
        return {"run_id": run_id, "consents_targeted": len(candidates), "mode": "reauthorize"}

    async def _run_reauthorize(self, run_id: str, candidates: list[dict],
                               concurrency: int) -> None:
        sem = asyncio.Semaphore(max(1, concurrency))
        started = time.perf_counter()
        totals = {"consents_revoked": 0, "consents_relinked": 0, "backfills_done": 0,
                  "snapshots_rebuilt": 0, "docs_erased": 0, "transactions": 0,
                  "calls": 0, "errors": 0}
        new_ids: list[str] = []  # freshly-minted consent_ids to reconcile from the ledger
        token = await self._client_token()

        async with httpx.AsyncClient(timeout=60) as hc:
            async def one(c):
                async with sem:
                    await self._reauth_one(hc, token, c, totals, new_ids)
                    await self._reconcile_backfills(new_ids, totals)
                    await self._persist_reauth(run_id, totals, started, "running")
            try:
                await asyncio.gather(*(one(c) for c in candidates))
                # the worker's backfills are async and can outlast the HTTP dance;
                # drain (bounded, scales with cohort) reconciling counts from the
                # pull ledger so the final numbers are authoritative.
                deadline = time.perf_counter() + 45 + 30 * len(new_ids)
                while time.perf_counter() < deadline:
                    await self._reconcile_backfills(new_ids, totals)
                    await self._persist_reauth(run_id, totals, started, "running")
                    if new_ids and totals["backfills_done"] >= len(new_ids):
                        break
                    await asyncio.sleep(3)
                await self._reconcile_backfills(new_ids, totals)
                await self._persist_reauth(run_id, totals, started, "completed")
            except Exception:
                await self._persist_reauth(run_id, totals, started, "failed")

    async def _reauth_one(self, hc: httpx.AsyncClient, token: str,
                          c: dict, totals: dict, new_ids: list) -> None:
        db, s = self.db, self.s
        cid_old = c["consent_id"]
        try:
            # 1. revoke via the real LCM endpoint -> publish -> worker gate_flip + erase
            r = await hc.post(f"{s.ofp_base_url}/v1/consents/{cid_old}/revoke",
                              json={"updated_by": "data_consumer_user"},
                              headers={"authorization": f"Bearer {token}"})
            if r.status_code == 201:
                await publish_consent_event(db, r.json())
                totals["consents_revoked"] += 1

            # 2. wait for the worker's chunked erasure to drain (bounded)
            totals["docs_erased"] += await self._await_erasure(cid_old)

            # 3. re-link (PAR -> authorize -> callback) -> worker backfills
            await self._relink_one(hc, c, totals, new_ids)
        except Exception:
            totals["errors"] += 1

    async def _relink_one(self, hc: httpx.AsyncClient, c: dict,
                          totals: dict, new_ids: list) -> bool:
        """Re-issue a (customer, dp, purpose) consent through the real flow:
        link -> PAR -> authorize (approve the consent's accounts) -> callback
        (token + publish). Mints a fresh consent_id; the worker then backfills it.
        Shared by reauthorize (after revoke) and reseed (re-link only)."""
        db, s = self.db, self.s
        customer_id = c.get("customer_id")
        dp_id = c["dp_id"]
        purpose = c["consent_purpose"]
        permissions = c.get("permissions", [])
        account_ids = [a["account_id"] for a in (c.get("accounts") or [])]
        if not account_ids:
            totals["errors"] += 1
            return False
        # snapshot the current version so we can spot the freshly-minted consent
        prev = await db.consents.find_one(
            {"customer_id": customer_id, "dp_id": dp_id, "consent_purpose": purpose},
            sort=[("_rcp_version", -1)], projection={"_rcp_version": 1})
        prev_ver = (prev or {}).get("_rcp_version", 0)

        lr = await hc.post(f"{s.dc_base_url}/consents/{customer_id}/link",
                           json={"dp_id": dp_id, "consent_purpose": purpose,
                                 "permissions": permissions, "validity_days": 180})
        if lr.status_code != 200:
            totals["errors"] += 1
            return False
        request_uri = parse_qs(urlparse(lr.json()["authorize_url"]).query)["request_uri"][0]
        dr = await hc.post(f"{s.ofp_base_url}/v1/oauth/authorize/decision",
                           data={"request_uri": request_uri, "decision": "approve",
                                 "account_ids": account_ids},
                           follow_redirects=False)
        if dr.status_code != 303:
            totals["errors"] += 1
            return False
        cb = await hc.get(dr.headers["location"])  # DC callback: token + publish
        if cb.status_code != 200:
            totals["errors"] += 1
            return False
        totals["consents_relinked"] += 1

        # discover the freshly-minted consent_id; the worker backfills it
        # asynchronously — counts are reconciled from the ledger (drain phase).
        new_id = await self._await_new_consent(customer_id, dp_id, purpose, prev_ver)
        if new_id:
            new_ids.append(new_id)
        return True

    # ----------------------------------------------------------------------
    # reseed mode — replenish data the storm erased: re-link REVOKED cohort
    # consents through the real flow (no revoke/erase step). New consent_ids are
    # URL-safe (mock minting fix), so the storm can revoke them afterwards.
    # ----------------------------------------------------------------------

    async def _start_reseed(self, count: int, concurrency: int, now) -> dict:
        db = self.db
        candidates = await db.consents.find({
            "status": "revoked",
            "customer_id": {"$ne": None},
            "accounts.0": {"$exists": True},
        }, CANDIDATE_PROJECTION).sort("_rcp_version", -1).limit(count * 3).to_list(None)
        # de-dup by (customer, dp, purpose) — relink each combo once
        seen, uniq = set(), []
        for c in candidates:
            key = (c.get("customer_id"), c.get("dp_id"), c.get("consent_purpose"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
            if len(uniq) >= count:
                break
        if not uniq:
            raise HTTPException(400, "no revoked consents to re-link — run a storm first")

        run_id = f"reseed_{uuid.uuid4().hex[:8]}"
        await db.simulation_runs.insert_one({
            "_id": run_id, "kind": "reseed_sim", "mode": "reseed", "started_at": now,
            "config": {"count": count, "concurrency": concurrency},
            "found": len(uniq), "status": "running",
            "consents_done": 0, "consents_revoked": 0, "consents_relinked": 0,
            "backfills_done": 0, "snapshots_rebuilt": 0, "docs_erased": 0,
            "transactions": 0, "calls": 0, "errors": 0,
            "elapsed_ms": 0, "throughput_tps": None,
        })
        asyncio.create_task(self._run_reseed(run_id, uniq, concurrency))
        return {"run_id": run_id, "consents_targeted": len(uniq), "mode": "reseed"}

    async def _run_reseed(self, run_id: str, candidates: list[dict],
                          concurrency: int) -> None:
        sem = asyncio.Semaphore(max(1, concurrency))
        started = time.perf_counter()
        totals = {"consents_revoked": 0, "consents_relinked": 0, "backfills_done": 0,
                  "snapshots_rebuilt": 0, "docs_erased": 0, "transactions": 0,
                  "calls": 0, "errors": 0}
        new_ids: list[str] = []

        async with httpx.AsyncClient(timeout=60) as hc:
            async def one(c):
                async with sem:
                    try:
                        await self._relink_one(hc, c, totals, new_ids)
                    except Exception:
                        totals["errors"] += 1
                    await self._reconcile_backfills(new_ids, totals)
                    await self._persist_reauth(run_id, totals, started, "running")
            try:
                await asyncio.gather(*(one(c) for c in candidates))
                deadline = time.perf_counter() + 45 + 30 * len(new_ids)
                while time.perf_counter() < deadline:
                    await self._reconcile_backfills(new_ids, totals)
                    await self._persist_reauth(run_id, totals, started, "running")
                    if new_ids and totals["backfills_done"] >= len(new_ids):
                        break
                    await asyncio.sleep(3)
                await self._reconcile_backfills(new_ids, totals)
                await self._persist_reauth(run_id, totals, started, "completed")
            except Exception:
                await self._persist_reauth(run_id, totals, started, "failed")

    async def _await_erasure(self, consent_id: str, timeout_s: float = 30) -> int:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            job = await self.db.erasure_jobs.find_one({"consent_id": consent_id})
            if job and job.get("status") == "completed":
                return int(job.get("docs_deleted", 0) or 0)
            await asyncio.sleep(0.5)
        return 0

    async def _await_new_consent(self, customer_id, dp_id, purpose,
                                 prev_ver: int, timeout_s: float = 25):
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            doc = await self.db.consents.find_one(
                {"customer_id": customer_id, "dp_id": dp_id, "consent_purpose": purpose,
                 "status": "authorized", "_rcp_version": {"$gt": prev_ver}},
                sort=[("_rcp_version", -1)], projection={"consent_id": 1})
            if doc:
                return doc["consent_id"]
            await asyncio.sleep(0.5)
        return None

    async def _reconcile_backfills(self, new_ids: list, totals: dict) -> None:
        """Authoritative ledger-derived counts for the worker's async backfills —
        recomputed (idempotent) so late backfills are captured without double count."""
        if not new_ids:
            return
        leds = await self.db.ofp_pull_ledger.find(
            {"consent_id": {"$in": new_ids}, "kind": "backfill"}).to_list(None)
        totals["backfills_done"] = len(leds)
        totals["transactions"] = sum(int(led.get("transactions", 0) or 0) for led in leds)
        totals["snapshots_rebuilt"] = sum(int(led.get("snapshots_rebuilt", 0) or 0) for led in leds)
        totals["calls"] = sum(int(led.get("calls", 0) or 0) for led in leds)

    async def _persist_reauth(self, run_id: str, totals: dict, started: float,
                              status: str) -> None:
        elapsed = time.perf_counter() - started
        upd = {
            **totals,
            # generic bar reads consents_done/found — point it at completed backfills
            "consents_done": totals["backfills_done"],
            "elapsed_ms": round(elapsed * 1000),
            "throughput_tps": (round(totals["transactions"] / elapsed, 1)
                               if elapsed > 0 else None),
            "status": status,
        }
        if status in ("completed", "failed"):
            upd["finished_at"] = datetime.now(timezone.utc)
        await self.db.simulation_runs.update_one({"_id": run_id}, {"$set": upd})

    async def status(self, run_id: str) -> dict:
        doc = await self.db.simulation_runs.find_one({"_id": run_id})
        if not doc:
            raise HTTPException(404, "unknown simulation run")
        return jsonable(doc)

    async def list_runs(self) -> dict:
        docs = await self.db.simulation_runs.find({}).sort("started_at", -1).limit(20).to_list(None)
        return {"runs": jsonable(docs)}
