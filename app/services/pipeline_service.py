from __future__ import annotations

from app.models.requests import AnalyzeRequest
from app.models.responses import AnalyzeResponse, CrossValidationResult, DebugContext
from app.services.cross_validation_service import CrossValidationService
from app.services.runtime import build_pipeline
from app.services.schema_service import SchemaService


class PipelineService:
    def __init__(self, schema_service: SchemaService) -> None:
        self._schema_service = schema_service
        self._cross_validator = CrossValidationService()

    def analyze(self, request: AnalyzeRequest) -> AnalyzeResponse:
        payload = request.payload
        admission_count = len(payload.admissions or ([] if payload.admission is None else [payload.admission]))
        pipeline = build_pipeline(request.resource_types)
        result = pipeline.run(payload.model_dump())
        cross_validation = self._cross_validator.validate(
            payload=payload,
            generated_resources=result.generated_resources,
            enabled=request.enable_cross_validation,
        )

        warnings = list(result.warnings)
        if payload.lab_event is None:
            warnings.append(
                "No lab_event was supplied; Observation generation may have limited evidence."
            )
        if result.verification_errors:
            warnings.append(
                "Pipeline returned verification errors; inspect the debug context before downstream use."
            )
        if cross_validation["summary"]["status"] == "failed":
            warnings.append(
                "Cross-validation did not fully pass; inspect generated resources against reference FHIR results."
            )

        debug_context = DebugContext(
            agent_sequence=[
                "Schema Agent",
                "Retrieval Agent",
                "Mapping Agent",
                "Validation Agent",
            ],
            rag_context=result.rag_context,
            schema_context=self._schema_service.build_schema_context(
                request.resource_types
            ),
            source_payload_summary={
                "patient_keys": sorted(payload.patient.keys()),
                "has_admission": admission_count > 0,
                "admission_count": admission_count,
                "has_lab_event": payload.lab_event is not None,
                "requested_resource_types": request.resource_types
                or ["Patient", "Encounter", "Observation"],
            },
            warnings=warnings,
        )

        return AnalyzeResponse(
            mapping_payload=result.mapping_payload,
            generated_resources=result.generated_resources,
            fhir_bundle=result.fhir_payload,
            verification_errors=result.verification_errors,
            attempts_used=result.attempts_used,
            corrections=result.corrections,
            debug_context=debug_context,
            cross_validation=CrossValidationResult.model_validate(cross_validation),
        )
