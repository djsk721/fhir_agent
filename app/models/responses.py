from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DebugContext(BaseModel):
    agent_sequence: list[str] = Field(default_factory=list)
    rag_context: list[dict[str, Any]] = Field(default_factory=list)
    schema_context: dict[str, Any] = Field(default_factory=dict)
    source_payload_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    mapping_payload: dict[str, Any]
    generated_resources: dict[str, dict[str, Any]]
    fhir_bundle: dict[str, Any]
    verification_errors: list[str]
    attempts_used: int
    debug_context: DebugContext


class MappingResponse(BaseModel):
    itemid: int | str | None = None
    mapping_payload: dict[str, Any]
    mapping_explanation: str
    debug_context: DebugContext


class SchemaResponse(BaseModel):
    source: str
    supported_resource_types: list[str]
    definitions: dict[str, Any]
