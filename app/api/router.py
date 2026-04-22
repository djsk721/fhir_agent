from __future__ import annotations

from fastapi import APIRouter

from app.api.analyze import router as analyze_router
from app.api.mapping import router as mapping_router
from app.api.schema import router as schema_router

api_router = APIRouter()
api_router.include_router(analyze_router)
api_router.include_router(mapping_router)
api_router.include_router(schema_router)
