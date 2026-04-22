from __future__ import annotations

from functools import lru_cache

from app.services.mapping_service import MappingService
from app.services.pipeline_service import PipelineService
from app.services.schema_service import SchemaService


@lru_cache(maxsize=1)
def get_schema_service() -> SchemaService:
    return SchemaService()


@lru_cache(maxsize=1)
def get_mapping_service() -> MappingService:
    return MappingService(schema_service=get_schema_service())


@lru_cache(maxsize=1)
def get_pipeline_service() -> PipelineService:
    return PipelineService(schema_service=get_schema_service())
