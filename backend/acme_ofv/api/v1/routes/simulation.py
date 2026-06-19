"""Transaction-ingestion simulator controllers — thin: delegate to
SimulationService. Drives the spec-faithful consumption path under load
(brief §6.2) with live, persisted run stats."""

from fastapi import APIRouter, Depends

from acme_ofv.api.v1.deps import get_simulation_service
from acme_ofv.models.requests import SimulationRequest
from acme_ofv.services.simulation_service import SimulationService

router = APIRouter(prefix="/ops")


@router.post("/simulation")
async def start_simulation(svc: SimulationService = Depends(get_simulation_service),
                           body: SimulationRequest | None = None):
    body = body or SimulationRequest()
    return await svc.start(body.count, body.concurrency, body.mode)


@router.get("/simulation")
async def list_simulations(svc: SimulationService = Depends(get_simulation_service)):
    return await svc.list_runs()


@router.get("/simulation/{run_id}")
async def simulation_status(run_id: str,
                            svc: SimulationService = Depends(get_simulation_service)):
    return await svc.status(run_id)
