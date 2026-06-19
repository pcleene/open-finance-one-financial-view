"""FastAPI dependency providers (DIP): controllers depend on injected services,
never on the global app.state.db directly."""

from fastapi import Request

from acme_ofv.search.service import SearchService
from acme_ofv.services.consent_service import ConsentService
from acme_ofv.services.one_view_service import OneViewService
from acme_ofv.services.ops_service import OpsService
from acme_ofv.services.pfm_service import PfmService
from acme_ofv.services.simulation_service import SimulationService
from acme_ofv.services.underwriting_service import UnderwritingService


def get_pfm_service(request: Request) -> PfmService:
    return PfmService(request.app.state.db)


def get_search_service(request: Request) -> SearchService:
    return SearchService(request.app.state.db)


def get_underwriting_service(request: Request) -> UnderwritingService:
    return UnderwritingService(request.app.state.db)


def get_one_view_service(request: Request) -> OneViewService:
    return OneViewService(request.app.state.db)


def get_consent_service(request: Request) -> ConsentService:
    return ConsentService(request.app)


def get_ops_service(request: Request) -> OpsService:
    return OpsService(request.app)


def get_simulation_service(request: Request) -> SimulationService:
    return SimulationService(request.app)
