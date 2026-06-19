"""One View + customers controllers — thin: delegate to OneViewService."""

from fastapi import APIRouter, Depends, HTTPException

from acme_ofv.api.v1.deps import get_one_view_service
from acme_ofv.services.one_view_service import OneViewService

router = APIRouter()


@router.get("/customers")
async def customers(svc: OneViewService = Depends(get_one_view_service)):
    return await svc.list_customers()


@router.get("/one-view/{customer_id}")
async def one_view(customer_id: str, svc: OneViewService = Depends(get_one_view_service)):
    result = await svc.one_view(customer_id)
    if result is None:
        raise HTTPException(404, "unknown customer")
    return result
