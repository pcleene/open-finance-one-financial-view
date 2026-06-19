"""Scale-ops controllers (SSE feeds, revocation storm, on-demand sync) — thin:
delegate to OpsService."""

from fastapi import APIRouter, Depends

from acme_ofv.api.v1.deps import get_ops_service
from acme_ofv.models.requests import StormRequest
from acme_ofv.services.ops_service import OpsService

router = APIRouter(prefix="/ops")


@router.get("/events")
async def ops_events(svc: OpsService = Depends(get_ops_service)):
    return svc.events_response()


@router.get("/metrics")
async def ops_metrics(svc: OpsService = Depends(get_ops_service)):
    return await svc.metrics()


@router.post("/storm")
async def revocation_storm(svc: OpsService = Depends(get_ops_service),
                           body: StormRequest | None = None):
    body = body or StormRequest()
    return await svc.storm(body.n, body.read_rps)


@router.get("/storm")
async def storm_history(svc: OpsService = Depends(get_ops_service)):
    return await svc.list_runs()


@router.get("/storm/{run_id}")
async def storm_status(run_id: str, svc: OpsService = Depends(get_ops_service)):
    return await svc.storm_status(run_id)


@router.post("/sync/{customer_id}")
async def trigger_sync(customer_id: str, svc: OpsService = Depends(get_ops_service)):
    return await svc.trigger_sync(customer_id)
