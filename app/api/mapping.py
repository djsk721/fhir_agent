from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.dependencies import get_mapping_service
from app.models.requests import MappingRequest
from app.models.responses import MappingResponse
from app.services.mapping_service import MappingService

router = APIRouter(tags=["mapping"])


@router.post("/mapping", response_model=MappingResponse)
def explain_mapping(
    request: MappingRequest,
    service: MappingService = Depends(get_mapping_service),
) -> MappingResponse:
    return service.explain_mapping(request)
