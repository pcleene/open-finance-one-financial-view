"""Eraser / profile-updater / feature-updater worker (brief §5.3, §7.3, §8).

Consumes the consents change stream (the tail of the RCP → Kafka → Mongo path)
and is the ONLY writer of consent boxes inside customer_profiles:

  authorized            → project boxes onto embedded accounts + run backfill
  suspended / expired   → gate-flip (ACID txn: box update + audit entry)
  revoked               → gate-flip, then chunked physical erasure
                          (2 000-_id batches off the covering index), with
                          per-batch metrics into erasure_jobs

Change streams persist resume tokens in stream_checkpoints → crash-resumable.

Erasure is fire-and-forget off the change stream (the resume token advances once
the gate-flip commits, before the bulk delete finishes), so a worker crash /
transient error mid-erasure would orphan rows. `erasure_sweeper` is the retry
guarantee: it re-drives any job left `running` (stale heartbeat) or `failed`,
and the delete is idempotent (it re-queries the *remaining* rows), so resuming
is always safe — no ACID transaction needed (and one would blow the 16 MB / 60 s
transaction limits and stall the read path).

Run:  uv run python -m acme_ofv.eraser.worker
"""

import asyncio
import os
import time
import traceback
from datetime import datetime, timedelta, timezone

from bson import Timestamp
from pymongo import ReturnDocument

from acme_ofv.config import settings
from acme_ofv.db import make_async_client, ofv_db
from acme_ofv.ingestion.service import backfill_consent, refresh_summary

S = settings()
BACKFILL_SEM = asyncio.Semaphore(8)
ERASE_SEM = asyncio.Semaphore(int(os.environ.get("ERASER_CONCURRENCY", S.eraser_concurrency)))

# erasure re-drive (reconciliation) knobs
ERASE_MAX_ATTEMPTS = int(os.environ.get("ERASER_MAX_ATTEMPTS", 5))
ERASE_STALE_SECONDS = int(os.environ.get("ERASER_STALE_SECONDS", 120))  # heartbeat age → re-drivable
ERASE_SWEEP_INTERVAL = int(os.environ.get("ERASER_SWEEP_INTERVAL", 60))

client = None
db = None


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------- checkpoints

async def load_token(name: str):
    doc = await db.stream_checkpoints.find_one({"_id": name})
    return (doc or {}).get("resume_token")


async def save_token(name: str, token) -> None:
    await db.stream_checkpoints.replace_one(
        {"_id": name},
        {"_id": name, "resume_token": token, "at": datetime.now(timezone.utc)},
        upsert=True)


# ------------------------------------------------------ consent box updates

async def project_boxes_for_consent(consent: dict) -> None:
    """Authorized path: rebuild the box list on every embedded account this
    consent touches (covers new consents on already-embedded accounts)."""
    from acme_ofv.ingestion.service import build_consent_boxes
    customer_id = consent.get("customer_id")
    if not customer_id:
        return
    for ref in consent.get("accounts") or []:
        boxes = await build_consent_boxes(db, ref["account_id"])
        await db.customer_profiles.update_one(
            {"_id": customer_id, "accounts.account_id": ref["account_id"]},
            {"$set": {"accounts.$.consents": boxes}})


async def gate_flip(consent: dict) -> float:
    """The synchronous ACID phase: from this commit every read path excludes
    the consent's accounts — regardless of how many rows await deletion."""
    customer_id = consent.get("customer_id")
    consent_id = consent["consent_id"]
    t0 = time.perf_counter()
    async with client.start_session() as sess:
        async with await sess.start_transaction():
            await db.customer_profiles.update_one(
                {"_id": customer_id},
                {"$set": {
                    "accounts.$[a].consents.$[c].status": consent["status"],
                    "accounts.$[a].consents.$[c].status_reason": consent.get("status_reason"),
                    "accounts.$[a].consents.$[c].updated_by": consent.get("updated_by"),
                    "accounts.$[a].consents.$[c]._rcp_version": consent.get("_rcp_version"),
                }},
                array_filters=[{"a.consents.consent_id": consent_id},
                               {"c.consent_id": consent_id}],
                session=sess)
            await db.consent_audit_log.insert_one({
                "consent_id": consent_id, "customer_id": customer_id,
                "dp_id": consent["dp_id"], "consent_purpose": consent["consent_purpose"],
                "action": "gate_flip", "status": consent["status"],
                "status_reason": consent.get("status_reason"),
                "updated_by": consent.get("updated_by"),
                "_rcp_version": consent.get("_rcp_version"),
                "at": datetime.now(timezone.utc),
                "flip_ms": round((time.perf_counter() - t0) * 1000, 2),
            }, session=sess)
    return (time.perf_counter() - t0) * 1000


# ----------------------------------------------------------------- erasure

async def erase_consent_data(consent: dict) -> None:
    """Entry from the change stream on `revoked`: record an erasure_job (the unit
    of work the sweeper can re-drive) then drive it. Only accounts NOT still
    covered by another authorized consent are erased (set difference, brief §5.3)."""
    async with ERASE_SEM:
        customer_id = consent.get("customer_id")
        now = datetime.now(timezone.utc)
        erase_set = []
        for ref in consent.get("accounts") or []:
            still_covered = await db.consents.find_one({
                "accounts.account_id": ref["account_id"],
                "consent_id": {"$ne": consent["consent_id"]},
                "status": "authorized",
                "expiration_datetime": {"$gt": now},
            }, {"_id": 1})
            if not still_covered:
                erase_set.append(ref["account_id"])

        job = {
            "consent_id": consent["consent_id"], "customer_id": customer_id,
            "dp_id": consent["dp_id"], "consent_purpose": consent.get("consent_purpose"),
            "accounts": erase_set, "status": "running",
            "created_at": now, "updated_at": now, "attempts": 1, "last_error": None,
            "docs_deleted": 0, "snapshots_deleted": 0, "batches": 0, "batch_ms_max": 0.0,
        }
        ins = await db.erasure_jobs.insert_one(job)
        job["_id"] = ins.inserted_id

        if not erase_set:
            await db.erasure_jobs.update_one(
                {"_id": job["_id"]},
                {"$set": {"status": "completed", "finished_at": datetime.now(timezone.utc),
                          "note": "all accounts still covered by another authorized consent"},
                 "$currentDate": {"updated_at": True}})
            return
        await _drive_erasure(job)


async def _drive_erasure(job: dict) -> None:
    """Idempotent, resumable erasure for one job — re-runnable to completion.

    The delete loop always re-queries the *remaining* rows for the erase-set, so
    a half-finished attempt simply continues (the tail $pull / delete_many are
    idempotent too). On any error the job is marked `failed` (not left stuck) so
    the sweeper retries it; `updated_at` is bumped every batch as a heartbeat so a
    healthy in-flight job is never mistaken for stale."""
    job_id, customer_id, erase_set = job["_id"], job["customer_id"], job["accounts"]
    try:
        while True:
            t0 = time.perf_counter()
            ids = [d["_id"] async for d in db.transactions.find(
                {"customer_id": customer_id, "account.account_id": {"$in": erase_set}},
                {"_id": 1}).limit(S.erase_batch_size)]
            if not ids:
                break
            res = await db.transactions.delete_many({"_id": {"$in": ids}})
            await db.erasure_jobs.update_one({"_id": job_id}, {
                "$inc": {"docs_deleted": res.deleted_count, "batches": 1},
                "$max": {"batch_ms_max": round((time.perf_counter() - t0) * 1000, 2)},
                "$currentDate": {"updated_at": True},   # heartbeat → not seen as stale
            })
            if len(ids) < S.erase_batch_size:
                break

        snap = await db.balance_snapshots.delete_many(
            {"meta.customer_id": customer_id, "meta.account_id": {"$in": erase_set}})
        await db.uw_features.update_one(
            {"_id": customer_id},
            {"$pull": {"accounts": {"account_id": {"$in": erase_set}}}})
        await db.customer_profiles.update_one(
            {"_id": customer_id},
            {"$pull": {"accounts": {"account_id": {"$in": erase_set},
                                    "is_internal": False}}})
        await refresh_summary(db, customer_id)

        done = await db.erasure_jobs.find_one_and_update(
            {"_id": job_id},
            {"$set": {"status": "completed", "finished_at": datetime.now(timezone.utc),
                      "snapshots_deleted": snap.deleted_count, "last_error": None},
             "$currentDate": {"updated_at": True}},
            return_document=ReturnDocument.AFTER)
        total = (done or job).get("docs_deleted", 0)
        await db.consent_audit_log.insert_one({
            "consent_id": job["consent_id"], "customer_id": customer_id,
            "dp_id": job.get("dp_id"), "consent_purpose": job.get("consent_purpose"),
            "action": "physical_erasure", "status": "revoked",
            "docs_deleted": total, "snapshots_deleted": snap.deleted_count,
            "accounts_erased": erase_set, "attempts": (done or job).get("attempts"),
            "at": datetime.now(timezone.utc),
        })
        log(f"erased {total} txns + {snap.deleted_count} snapshots for "
            f"{job['consent_id']} ({len(erase_set)} accounts, attempt {(done or job).get('attempts')})")
    except Exception:
        await db.erasure_jobs.update_one({"_id": job_id}, {
            "$set": {"status": "failed", "last_error": traceback.format_exc()[-600:]},
            "$currentDate": {"updated_at": True}})
        log(f"erasure {job.get('consent_id')} FAILED (attempt {job.get('attempts')}) "
            f"— will be re-driven by the sweeper:\n{traceback.format_exc()}")


async def _resume_erasure(job: dict) -> None:
    async with ERASE_SEM:
        await _drive_erasure(job)


async def erasure_sweeper() -> None:
    """Reconciliation / retry guarantee for erasure (the property that makes
    fire-and-forget safe). Atomically claims any job left `running` with a stale
    heartbeat — i.e. the worker died mid-delete — or `failed`, under the attempt
    ceiling, then resumes it. Idempotent, so re-driving is always safe."""
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=ERASE_STALE_SECONDS)
            while True:
                # claim-then-drive: bumping updated_at + attempts under the same
                # filter is the lease — a claimed job won't be re-claimed for
                # ERASE_STALE_SECONDS, and _drive_erasure heartbeats while it runs.
                job = await db.erasure_jobs.find_one_and_update(
                    {"status": {"$in": ["running", "failed"]},
                     "attempts": {"$lt": ERASE_MAX_ATTEMPTS},
                     "updated_at": {"$lt": cutoff}},
                    {"$set": {"status": "running"}, "$inc": {"attempts": 1},
                     "$currentDate": {"updated_at": True}},
                    return_document=ReturnDocument.AFTER)
                if not job:
                    break
                log(f"sweeper re-driving erasure {job['consent_id']} "
                    f"(attempt {job['attempts']}, {job.get('docs_deleted', 0)} deleted so far)")
                asyncio.create_task(_resume_erasure(job))
        except Exception as exc:
            log(f"erasure sweeper error: {exc}")
        await asyncio.sleep(ERASE_SWEEP_INTERVAL)


# ----------------------------------------------------------- backfill path

_inflight: set[str] = set()


async def run_backfill(consent: dict) -> None:
    cid = consent["consent_id"]
    if cid in _inflight:
        return
    _inflight.add(cid)
    try:
        async with BACKFILL_SEM:
            prior = await db.ofp_pull_ledger.find_one({"consent_id": cid, "kind": "backfill"})
            if prior:
                await project_boxes_for_consent(consent)   # reactivation: boxes only
                await refresh_summary(db, consent.get("customer_id"))
                return
            stats = await backfill_consent(db, consent)
            await project_boxes_for_consent(consent)
            log(f"backfill {cid}: {stats}")
    except Exception:
        log(f"backfill {cid} failed:\n{traceback.format_exc()}")
    finally:
        _inflight.discard(cid)


# --------------------------------------------------------- event dispatch

async def handle_consent_event(consent: dict) -> None:
    if not consent.get("customer_id") and consent.get("hashed_id_number"):
        profile = await db.customer_profiles.find_one(
            {"customer.hashed_id_number": consent["hashed_id_number"]}, {"_id": 1})
        if profile:
            consent["customer_id"] = profile["_id"]
            await db.consents.update_one(
                {"_id": consent["consent_id"]},
                {"$set": {"customer_id": profile["_id"]}})

    status = consent["status"]
    await db.consent_audit_log.insert_one({
        "consent_id": consent["consent_id"], "customer_id": consent.get("customer_id"),
        "dp_id": consent["dp_id"], "consent_purpose": consent["consent_purpose"],
        "action": "event_received", "status": status,
        "updated_by": consent.get("updated_by"),
        "_rcp_version": consent.get("_rcp_version"),
        "at": datetime.now(timezone.utc),
    })

    if status == "authorized":
        asyncio.create_task(run_backfill(consent))
    elif status in ("revoked", "expired", "suspended"):
        flip_ms = await gate_flip(consent)
        await refresh_summary(db, consent.get("customer_id"))
        log(f"gate flip {consent['consent_id']} → {status} in {flip_ms:.1f} ms")
        if status == "revoked":
            asyncio.create_task(erase_consent_data(consent))


async def consents_watcher() -> None:
    while True:
        try:
            token = await load_token("consents_watch")
            kwargs = {"full_document": "updateLookup"}
            if token:
                kwargs["resume_after"] = token
            async with await db.consents.watch(
                [{"$match": {"operationType": {"$in": ["insert", "replace", "update"]}}}],
                **kwargs,
            ) as stream:
                log("consents change stream open")
                async for change in stream:
                    doc = change.get("fullDocument")
                    if doc:
                        try:
                            await handle_consent_event(doc)
                        except Exception:
                            log(f"handler error:\n{traceback.format_exc()}")
                    await save_token("consents_watch", change["_id"])
        except Exception as exc:
            log(f"consents watcher reconnect after error: {exc}")
            await db.stream_checkpoints.delete_one({"_id": "consents_watch"})
            await asyncio.sleep(2)


# ------------------------------------------------ uw_features live updater

async def transactions_watcher() -> None:
    """Live feed (acme.core.transactions equivalent): per-insert $inc of the
    affected account's monthly bucket — decomposable components only."""
    while True:
        try:
            async with await db.transactions.watch(
                [{"$match": {"operationType": "insert",
                             "fullDocument.ingest.source": "acme_core_live"}}],
                full_document="updateLookup",
            ) as stream:
                log("transactions change stream open (live feed)")
                async for change in stream:
                    t = change["fullDocument"]
                    month = t["enrichment"]["month"]
                    field = "inflow" if t["credit_debit_indicator"] == "credit" else "outflow"
                    amt = t["amount"]["amount"].to_decimal()
                    res = await db.uw_features.update_one(
                        {"_id": t["customer_id"]},
                        {"$inc": {f"accounts.$[a].monthly.$[m].{field}": amt,
                                  "accounts.$[a].monthly.$[m].txn_count": 1}},
                        array_filters=[{"a.account_id": t["account"]["account_id"]},
                                       {"m.month": month}])
                    if res.modified_count == 0:
                        pass  # bucket not present yet — nightly true-up reconciles
        except Exception as exc:
            log(f"transactions watcher reconnect: {exc}")
            await asyncio.sleep(2)


# ------------------------------------------------------------ expiry sweep

async def expiry_sweeper() -> None:
    """Bookkeeping only — enforcement is the $gt comparison in the gate."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            cursor = db.consents.find(
                {"status": "authorized", "expiration_datetime": {"$lt": now}})
            async for c in cursor:
                await db.consents.update_one(
                    {"_id": c["_id"], "status": "authorized"},
                    {"$set": {"status": "expired", "updated_at": now,
                              "updated_by": "ofp",
                              "_rcp_version": c.get("_rcp_version", 0) + 1}})
                # change stream fires → gate flip + audit follow automatically
        except Exception as exc:
            log(f"expiry sweeper error: {exc}")
        await asyncio.sleep(60)


async def main() -> None:
    global client, db
    client = make_async_client(max_pool=80)
    db = ofv_db(client)
    log(f"worker up — db={db.name}, erase_batch={S.erase_batch_size}, "
        f"erase_sweep={ERASE_SWEEP_INTERVAL}s/stale={ERASE_STALE_SECONDS}s")
    await asyncio.gather(consents_watcher(), transactions_watcher(),
                         expiry_sweeper(), erasure_sweeper())


if __name__ == "__main__":
    asyncio.run(main())
