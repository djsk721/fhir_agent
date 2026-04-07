from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import FhirGeneratorAgent, ManagerAgent, MappingAgent, VerifierAgent
from .config import Settings
from .db import PatientDemoRow
from .fhir_models import build_bundle, validate_bundle, validate_fhir_json


@dataclass(frozen=True)
class PipelineResult:
    """파이프라인 실행 결과를 담습니다."""

    mapping_payload: dict[str, Any]
    generated_resources: dict[str, dict[str, Any]]
    fhir_payload: dict[str, Any]
    verification_errors: list[str]
    attempts_used: int


class MultiAgentFhirPipeline:
    """MIMIC-IV 로우를 FHIR JSON으로 변환하는 다중 에이전트 파이프라인입니다."""

    _resource_order = {
        "Patient": 0,
        "Encounter": 1,
        "Observation": 2,
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._manager = ManagerAgent(settings)
        self._mapper = MappingAgent(settings)
        self._generator = FhirGeneratorAgent(settings)
        self._verifier = VerifierAgent(settings)

    def _ordered_resource_types(self) -> list[str]:
        """의존 관계를 고려해 리소스 생성 순서를 정렬합니다."""

        return sorted(
            self._settings.fhir_output_resources,
            key=lambda resource_type: self._resource_order.get(resource_type, 99),
        )

    def run(self, patient_demo_row: PatientDemoRow) -> PipelineResult:
        """매핑, 생성, 검증, 자기수정 루프를 순차 실행합니다."""

        raw_payload = patient_demo_row.as_dict()
        mapping_payload = self._mapper.run(raw_payload)

        generated_resources: dict[str, dict[str, Any]] = {}
        final_errors: list[str] = []
        total_attempts = 0

        for resource_type in self._ordered_resource_types():
            correction_instructions = (
                f"Generate a valid {resource_type} resource using only the provided evidence."
            )
            candidate: dict[str, Any] = {}
            resource_success = False

            for attempt in range(1, self._settings.max_self_correction_attempts + 2):
                total_attempts += 1
                candidate = self._generator.run(
                    mapping_payload=mapping_payload,
                    resource_type=resource_type,
                    correction_instructions=correction_instructions,
                    generated_resource_context=generated_resources,
                )

                llm_verification = self._verifier.run(
                    fhir_payload=candidate,
                    resource_type=resource_type,
                )
                schema_ok, schema_errors = validate_fhir_json(
                    resource=candidate,
                    resource_type=resource_type,
                )

                combined_errors = list(llm_verification.errors) + list(schema_errors)
                if llm_verification.ok and schema_ok:
                    generated_resources[resource_type] = candidate
                    resource_success = True
                    break

                if attempt > self._settings.max_self_correction_attempts:
                    final_errors.extend(
                        [f"{resource_type}: {error}" for error in combined_errors]
                    )
                    break

                manager_decision = self._manager.review_feedback(
                    verifier_result=llm_verification,
                    current_fhir_payload=candidate,
                    resource_type=resource_type,
                )
                correction_instructions = manager_decision.get("correction_instructions") or (
                    f"Fix all {resource_type} verifier issues and return valid JSON only."
                )

            if not resource_success:
                partial_bundle = build_bundle(generated_resources)
                return PipelineResult(
                    mapping_payload=mapping_payload,
                    generated_resources=generated_resources,
                    fhir_payload=partial_bundle,
                    verification_errors=final_errors,
                    attempts_used=total_attempts,
                )

        final_bundle = build_bundle(generated_resources)
        bundle_ok, bundle_errors = validate_bundle(
            bundle=final_bundle,
            required_resource_types=self._ordered_resource_types(),
        )
        if not bundle_ok:
            final_errors.extend(bundle_errors)

        return PipelineResult(
            mapping_payload=mapping_payload,
            generated_resources=generated_resources,
            fhir_payload=final_bundle,
            verification_errors=final_errors,
            attempts_used=total_attempts,
        )
