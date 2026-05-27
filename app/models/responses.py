from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DebugContext(BaseModel):
    agent_sequence: list[str] = Field(default_factory=list)
    rag_context: list[dict[str, Any]] = Field(default_factory=list)
    schema_context: dict[str, Any] = Field(default_factory=dict)
    source_payload_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class CrossValidationDifference(BaseModel):
    path: str
    expected: Any = None
    actual: Any = None


class CrossValidationItem(BaseModel):
    resource_type: str
    generated_id: str | None = None
    generated_resource: dict[str, Any] = Field(default_factory=dict)
    reference_file: str | None = None
    reference_line: int | None = None
    reference_resource: dict[str, Any] | None = None
    status: str
    differences: list[CrossValidationDifference] = Field(default_factory=list)
    skipped_reason: str | None = None


class CrossValidationSummary(BaseModel):
    status: str
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    skipped: int = 0


class CrossValidationResult(BaseModel):
    summary: CrossValidationSummary
    items: list[CrossValidationItem] = Field(default_factory=list)
    reference_directory: str


class AnalyzeResponse(BaseModel):
    mapping_payload: dict[str, Any]
    generated_resources: dict[str, Any]
    fhir_bundle: dict[str, Any]
    verification_errors: list[str]
    attempts_used: int
    corrections: list[dict[str, str]] = Field(default_factory=list)
    debug_context: DebugContext
    cross_validation: CrossValidationResult | None = None


class MappingResponse(BaseModel):
    itemid: int | str | None = None
    mapping_payload: dict[str, Any]
    mapping_explanation: str
    debug_context: DebugContext


class SchemaResponse(BaseModel):
    source: str
    supported_resource_types: list[str]
    definitions: dict[str, Any]
