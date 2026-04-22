from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.dependencies import get_pipeline_service
from app.models.requests import AnalyzeRequest
from app.models.responses import AnalyzeResponse
from app.services.pipeline_service import PipelineService

router = APIRouter(tags=["analysis"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze_mimic_payload(
    request: AnalyzeRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> AnalyzeResponse:
    return service.analyze(request)
