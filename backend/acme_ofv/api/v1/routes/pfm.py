"""PFM controllers (brief §9) — thin: delegate to PfmService / SearchService."""

from fastapi import APIRouter, Depends

from acme_ofv.api.serialize import jsonable
from acme_ofv.api.v1.deps import get_pfm_service, get_search_service
from acme_ofv.models.requests import BudgetsRequest
from acme_ofv.search.service import SearchService
from acme_ofv.services.pfm_service import PfmService

router = APIRouter(prefix="/pfm")


@router.get("/{customer_id}/spend")
async def spend(customer_id: str, month: str | None = None,
                svc: PfmService = Depends(get_pfm_service)):
    return await svc.spend_by_category(customer_id, month)


@router.get("/{customer_id}/cashflow")
async def cashflow(customer_id: str, months: int = 12, institution: str | None = None,
                   indicator: str | None = None, svc: PfmService = Depends(get_pfm_service)):
    return await svc.cashflow(customer_id, months, institution, indicator)


@router.get("/{customer_id}/net-worth")
async def net_worth(customer_id: str, weeks: int = 13,
                    svc: PfmService = Depends(get_pfm_service)):
    return await svc.net_worth(customer_id, weeks)


@router.get("/{customer_id}/recurring")
async def recurring(customer_id: str, svc: PfmService = Depends(get_pfm_service)):
    return await svc.recurring(customer_id)


@router.get("/{customer_id}/merchants")
async def merchants(customer_id: str, month: str | None = None,
                    svc: PfmService = Depends(get_pfm_service)):
    return await svc.top_merchants(customer_id, month)


@router.get("/{customer_id}/transactions")
async def transactions(customer_id: str, category: str | None = None,
                       institution: str | None = None, account_id: str | None = None,
                       indicator: str | None = None, min_amount: float | None = None,
                       max_amount: float | None = None, from_date: str | None = None,
                       to_date: str | None = None, cursor: str | None = None,
                       page_size: int = 50, svc: PfmService = Depends(get_pfm_service)):
    return await svc.transactions(customer_id, category, institution, account_id, indicator,
                                  min_amount, max_amount, from_date, to_date, cursor, page_size)


@router.get("/{customer_id}/transactions/search")
async def search_transactions(customer_id: str, q: str = "", mode: str = "hybrid",
                              page_size: int = 50, page: int = 1,
                              w_vec: float = 0.7, w_text: float = 0.3,
                              pfm: PfmService = Depends(get_pfm_service),
                              search: SearchService = Depends(get_search_service)):
    """Hybrid transaction search (brief §5): $rankFusion(text + vector) with the
    $rankFusion -> $vectorSearch -> $search -> app-side RRF fallback chain.
    Consent-gated to the pfm scope; the scope is applied to BOTH legs."""
    allowed = await pfm.require_pfm_scope(customer_id)
    return jsonable(await search.search(customer_id, allowed, q, mode=mode,
                                        page_size=page_size, page=page,
                                        w_vec=w_vec, w_text=w_text))


@router.get("/{customer_id}/budgets")
async def get_budgets(customer_id: str, svc: PfmService = Depends(get_pfm_service)):
    return await svc.get_budgets(customer_id)


@router.put("/{customer_id}/budgets")
async def put_budgets(customer_id: str, body: BudgetsRequest,
                      svc: PfmService = Depends(get_pfm_service)):
    return await svc.put_budgets(customer_id, body.budgets)


@router.get("/{customer_id}/safe-to-spend")
async def safe_to_spend(customer_id: str, svc: PfmService = Depends(get_pfm_service)):
    return await svc.safe_to_spend(customer_id)


@router.get("/{customer_id}/commitments")
async def commitments(customer_id: str, svc: PfmService = Depends(get_pfm_service)):
    return await svc.commitments_calendar(customer_id)


@router.get("/{customer_id}/utilization")
async def utilization(customer_id: str, svc: PfmService = Depends(get_pfm_service)):
    return await svc.credit_utilization(customer_id)


@router.get("/{customer_id}/money-map")
async def money_map(customer_id: str, month: str | None = None,
                    svc: PfmService = Depends(get_pfm_service)):
    return await svc.money_map(customer_id, month)
