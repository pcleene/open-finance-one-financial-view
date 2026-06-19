"""Consent centre + PAR/authorize link flow + the Open Finance platform webhook controllers —
thin: delegate to ConsentService."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from acme_ofv.api.v1.deps import get_consent_service
from acme_ofv.models.requests import LinkRequest
from acme_ofv.services.consent_service import ConsentService

router = APIRouter()


@router.get("/consents/{customer_id}")
async def consent_centre(customer_id: str,
                         svc: ConsentService = Depends(get_consent_service)):
    return await svc.consent_centre(customer_id)


@router.post("/consents/{customer_id}/link")
async def link_institution(customer_id: str, body: LinkRequest,
                           svc: ConsentService = Depends(get_consent_service)):
    return await svc.link(customer_id, body)


@router.get("/consents-callback")
async def link_callback(state: str, code: str | None = None, error: str | None = None,
                        svc: ConsentService = Depends(get_consent_service)):
    return HTMLResponse(await svc.link_callback(state, code, error))


@router.post("/consents/action/{consent_path:path}")
async def consent_action(consent_path: str,
                         svc: ConsentService = Depends(get_consent_service)):
    # consent_id may contain '/' (e.g. NPF) — capture the tail and split the
    # trailing action so the embedded slash doesn't break route matching.
    consent_id, _, action = consent_path.rpartition("/")
    return await svc.action(consent_id, action)


@router.post("/v1/consents/events")
async def consent_webhook(request: Request,
                          svc: ConsentService = Depends(get_consent_service)):
    event = await request.json()
    return await svc.webhook(event)
