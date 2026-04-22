from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

from fhir_agent_pipeline.config import Settings, load_settings
from fhir_agent_pipeline.pipeline import MultiAgentFhirPipeline
from fhir_agent_pipeline.retrieval import RuntimeRetriever
from fhir_agent_pipeline.sql_runtime import SafeSqlRuntime


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def get_runtime_retriever() -> RuntimeRetriever:
    return RuntimeRetriever(get_settings())


@lru_cache(maxsize=1)
def get_sql_runtime() -> SafeSqlRuntime:
    return SafeSqlRuntime(get_settings())


def get_settings_with_resources(resource_types: list[str] | None) -> Settings:
    settings = get_settings()
    if not resource_types:
        return settings
    return replace(
        settings,
        fhir_target_resources=resource_types,
        fhir_output_resources=resource_types,
    )


def build_pipeline(resource_types: list[str] | None = None) -> MultiAgentFhirPipeline:
    return MultiAgentFhirPipeline(
        get_settings_with_resources(resource_types),
        retriever=get_runtime_retriever(),
        sql_runtime=get_sql_runtime(),
    )
