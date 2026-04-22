from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_schema_service
from app.models.responses import SchemaResponse
from app.services.schema_service import SchemaService

router = APIRouter(tags=["schema"])


@router.get("/schema", response_model=SchemaResponse)
def get_schema(
    resource_types: list[str] | None = Query(default=None),
    service: SchemaService = Depends(get_schema_service),
) -> SchemaResponse:
    return service.get_schema(resource_types)
