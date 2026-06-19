"""Aggregates the v1 route groups (brief §6, OCP: add a route module + register
here — never edit a god-file). Mounted at the app root so spec paths like
/v1/consents/events and /one-view/{id} are preserved."""

from fastapi import APIRouter

from acme_ofv.api.v1.routes import consent, one_view, ops, pfm, simulation, underwriting

api_router = APIRouter()
api_router.include_router(one_view.router)
api_router.include_router(pfm.router)
api_router.include_router(underwriting.router)
api_router.include_router(consent.router)
api_router.include_router(ops.router)
api_router.include_router(simulation.router)
