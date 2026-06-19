"""Underwriting controllers (brief §10) — thin: delegate to UnderwritingService."""

from fastapi import APIRouter, Depends, Request

from acme_ofv.api.v1.deps import get_underwriting_service
from acme_ofv.services.underwriting_service import UnderwritingService

router = APIRouter(prefix="/underwriting")


@router.post("/{customer_id}/run")
async def run_scorecard(customer_id: str, request: Request,
                        svc: UnderwritingService = Depends(get_underwriting_service)):
    body = await request.json() if (await request.body()) else {}
    return await svc.run(customer_id, body.get("product", "personal_loan_50k_60m"))


@router.get("/{customer_id}/runs")
async def run_history(customer_id: str,
                      svc: UnderwritingService = Depends(get_underwriting_service)):
    return await svc.runs(customer_id)


@router.get("/{customer_id}/features")
async def features(customer_id: str,
                   svc: UnderwritingService = Depends(get_underwriting_service)):
    return await svc.features(customer_id)
