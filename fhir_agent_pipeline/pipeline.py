from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from .agents import (
    FhirGeneratorAgent,
    ManagerAgent,
    MappingAgent,
    SqlPlannerAgent,
    VerificationResult,
    VerifierAgent,
)
from .config import Settings
from .db import PatientDemoRow
from .fhir_models import build_bundle, validate_bundle, validate_fhir_json
from .retrieval import RetrievalContext, RuntimeRetriever
from .sql_runtime import SafeSqlRuntime, merge_sql_evidence


@dataclass(frozen=True)
class PipelineResult:
    """파이프라인 실행 결과를 담습니다."""

    mapping_payload: dict[str, Any]
    generated_resources: dict[str, dict[str, Any]]
    fhir_payload: dict[str, Any]
    verification_errors: list[str]
    attempts_used: int
    rag_context: list[dict[str, Any]]
    sql_evidence: dict[str, list[dict[str, Any]]]


class MultiAgentFhirPipeline:
    """MIMIC-IV 로우를 FHIR JSON으로 변환하는 다중 에이전트 파이프라인입니다."""

    _resource_order = {
        "Patient": 0,
        "Encounter": 1,
        "Observation": 2,
    }

    def __init__(
        self,
        settings: Settings,
        retriever: RuntimeRetriever | None = None,
        sql_runtime: SafeSqlRuntime | None = None,
    ) -> None:
        self._settings = settings
        self._retriever = retriever
        self._sql_runtime = sql_runtime
        self._manager = ManagerAgent(settings)
        self._mapper = MappingAgent(settings)
        self._sql_planner = SqlPlannerAgent(settings)
        self._generator = FhirGeneratorAgent(settings)
        self._verifier = VerifierAgent(settings)

    def _ordered_resource_types(self) -> list[str]:
        """의존 관계를 고려해 리소스 생성 순서를 정렬합니다."""

        return sorted(
            self._settings.fhir_target_resources,
            key=lambda resource_type: self._resource_order.get(resource_type, 99),
        )

    def run(self, patient_demo_row: PatientDemoRow | dict[str, Any]) -> PipelineResult:
        """매핑, 생성, 검증, 자기수정 루프를 순차 실행합니다."""

        raw_payload = (
            patient_demo_row.as_dict()
            if isinstance(patient_demo_row, PatientDemoRow)
            else patient_demo_row
        )
        retrieval_context = self._build_retrieval_context(
            raw_payload=raw_payload,
            resource_types=self._ordered_resource_types(),
        )
        sql_evidence, sql_trace = self._collect_sql_evidence(
            raw_payload=raw_payload,
            retrieval_context=retrieval_context,
        )
        enriched_payload = merge_sql_evidence(raw_payload, sql_evidence)
        mapping_payload = self._mapper.run(
            enriched_payload,
            rag_context=retrieval_context.mapping_prompt_context,
            sql_evidence=sql_evidence,
        )

        generated_resources: dict[str, dict[str, Any]] = {}
        final_errors: list[str] = []
        total_attempts = 0

        for resource_type in self._ordered_resource_types():
            correction_instructions = f"Generate a valid {resource_type} resource using only the provided evidence."
            candidate: dict[str, Any] = {}
            resource_success = False

            for attempt in range(1, self._settings.max_self_correction_attempts + 2):
                total_attempts += 1
                candidate = self._generator.run(
                    mapping_payload=mapping_payload,
                    resource_type=resource_type,
                    correction_instructions=correction_instructions,
                    generated_resource_context=generated_resources,
                    rag_context=retrieval_context.resource_prompt_context.get(
                        resource_type, retrieval_context.mapping_prompt_context
                    ),
                )
                if resource_type == "Patient":
                    candidate = normalize_patient_resource(candidate, raw_payload)
                elif resource_type == "Encounter":
                    candidate = normalize_encounter_resource(candidate, enriched_payload)

                llm_verification = self._verifier.run(
                    fhir_payload=candidate,
                    resource_type=resource_type,
                    rag_context=retrieval_context.resource_prompt_context.get(
                        resource_type, retrieval_context.mapping_prompt_context
                    ),
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

                manager_feedback = VerificationResult(
                    ok=llm_verification.ok and schema_ok,
                    errors=combined_errors,
                    suggestions=list(llm_verification.suggestions),
                )
                manager_decision = self._manager.review_feedback(
                    verifier_result=manager_feedback,
                    current_fhir_payload=candidate,
                    resource_type=resource_type,
                )
                correction_instructions = manager_decision.get(
                    "correction_instructions"
                ) or (
                    f"Fix all {resource_type} validation issues and return valid JSON only."
                )

            if not resource_success:
                partial_bundle = build_bundle(generated_resources)
                return PipelineResult(
                    mapping_payload=mapping_payload,
                    generated_resources=generated_resources,
                    fhir_payload=partial_bundle,
                    verification_errors=final_errors,
                    attempts_used=total_attempts,
                    rag_context=sql_trace + retrieval_context.trace,
                    sql_evidence=sql_evidence,
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
            rag_context=sql_trace + retrieval_context.trace,
            sql_evidence=sql_evidence,
        )

    def _build_retrieval_context(
        self, raw_payload: dict[str, Any], resource_types: list[str]
    ) -> RetrievalContext:
        if self._retriever is None:
            return RetrievalContext(
                mapping_prompt_context="No retrieval runtime configured.",
                resource_prompt_context={
                    resource_type: "No retrieval runtime configured."
                    for resource_type in resource_types
                },
                trace=[
                    {
                        "type": "runtime",
                        "status": "disabled",
                        "corpus": "all",
                        "detail": "No runtime retriever instance was configured for the pipeline.",
                    }
                ],
            )
        return self._retriever.build_context(raw_payload, resource_types)

    def _collect_sql_evidence(
        self,
        raw_payload: dict[str, Any],
        retrieval_context: RetrievalContext,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        if self._sql_runtime is None or not self._settings.sql_enabled:
            return {}, [
                {
                    "type": "sql_plan",
                    "status": "disabled",
                    "detail": "SQL runtime is disabled or not configured.",
                }
            ]

        evidence: dict[str, list[dict[str, Any]]] = {}
        traces: list[dict[str, Any]] = []

        for resource_type in self._ordered_resource_types():
            seed_context = self._sql_runtime.build_sql_seed_context(
                raw_payload, resource_type
            )
            sql_schema_context = self._sql_runtime.build_schema_context(resource_type)
            rag_context = retrieval_context.resource_prompt_context.get(
                resource_type,
                retrieval_context.mapping_prompt_context,
            )

            try:
                plan = self._sql_planner.run(
                    resource_type=resource_type,
                    seed_context=seed_context,
                    sql_schema_context=sql_schema_context,
                    rag_context=rag_context,
                )
                execution = self._sql_runtime.execute_plan(
                    plan=self._sql_runtime_plan(plan, resource_type)
                )
                evidence[resource_type] = execution.rows
                traces.extend(execution.trace)
            except Exception as exc:
                if not self._settings.sql_fail_open:
                    raise
                evidence[resource_type] = []
                traces.append(
                    {
                        "type": "sql_plan",
                        "resource_type": resource_type,
                        "status": "error",
                        "detail": str(exc),
                    }
                )

        return evidence, traces

    def _sql_runtime_plan(self, plan: Any, resource_type: str) -> Any:
        from .sql_runtime import SqlQueryPlan

        return SqlQueryPlan(
            resource_type=resource_type,
            sql=plan.sql,
            params=plan.params,
            tables=plan.tables,
            rationale=plan.rationale,
        )


_FHIR_ADMINISTRATIVE_GENDERS = {"male", "female", "other", "unknown"}
_FHIR_ENCOUNTER_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
_FHIR_ENCOUNTER_CLASS_DISPLAY = {
    "AMB": "ambulatory",
    "EMER": "emergency",
    "IMP": "inpatient encounter",
    "OBSENC": "observation encounter",
}


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _normalize_patient_gender(raw_gender: Any) -> str | None:
    if raw_gender is None:
        return None
    gender = str(raw_gender).strip().lower()
    if not gender:
        return None
    if gender in {"m", "male"}:
        return "male"
    if gender in {"f", "female"}:
        return "female"
    if gender == "other":
        return "other"
    if gender in {"u", "unk", "unknown", "none", "null", "na", "n/a"}:
        return "unknown"
    return None


def _normalize_patient_birth_date(raw_patient: dict[str, Any], current_birth_date: Any) -> str | None:
    if isinstance(current_birth_date, str):
        try:
            date.fromisoformat(current_birth_date)
            return current_birth_date
        except ValueError:
            pass

    for key in ("birthDate", "birth_date", "dob", "date_of_birth"):
        raw_birth_date = raw_patient.get(key)
        if isinstance(raw_birth_date, str):
            try:
                date.fromisoformat(raw_birth_date)
                return raw_birth_date
            except ValueError:
                continue

    anchor_year = _parse_int(raw_patient.get("anchor_year"))
    anchor_age = _parse_int(raw_patient.get("anchor_age"))
    if anchor_year is None or anchor_age is None or anchor_age < 0:
        return None

    birth_year = anchor_year - anchor_age
    if birth_year < 1 or birth_year > 9999:
        return None
    return f"{birth_year:04d}-01-01"


def normalize_patient_resource(
    resource: dict[str, Any], raw_payload: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return resource

    normalized = dict(resource)
    raw_patient = raw_payload.get("patient") if isinstance(raw_payload, dict) else None
    raw_patient = raw_patient if isinstance(raw_patient, dict) else {}

    current_gender = normalized.get("gender")
    if current_gender not in _FHIR_ADMINISTRATIVE_GENDERS:
        normalized_gender = _normalize_patient_gender(raw_patient.get("gender"))
        if normalized_gender is None:
            normalized.pop("gender", None)
        else:
            normalized["gender"] = normalized_gender

    normalized_birth_date = _normalize_patient_birth_date(
        raw_patient, normalized.get("birthDate")
    )
    if normalized_birth_date is None:
        normalized.pop("birthDate", None)
    else:
        normalized["birthDate"] = normalized_birth_date

    return normalized


def _extract_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        values: list[str] = []
        for nested_value in value.values():
            values.extend(_extract_text_values(nested_value))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_extract_text_values(item))
        return values
    return []


def _has_any_key(source: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(source.get(key) not in (None, "", []) for key in keys)


def _looks_like_valid_encounter_class(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    code = value.get("code")
    return isinstance(code, str) and bool(code.strip())


def _contains_phrase(text: str, phrase: str) -> bool:
    if " " in phrase or "-" in phrase:
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _derive_encounter_class_code_from_sources(sources: list[dict[str, Any]]) -> str | None:
    text_values: list[str] = []
    has_ed_timestamps = False

    for source in sources:
        if not isinstance(source, dict):
            continue
        text_values.extend(_extract_text_values(source))
        has_ed_timestamps = has_ed_timestamps or _has_any_key(
            source,
            (
                "edregtime",
                "edouttime",
                "emergency_reg_time",
                "emergency_out_time",
            ),
        )

    normalized_text = " ".join(value.lower() for value in text_values)
    if has_ed_timestamps or any(
        _contains_phrase(normalized_text, token)
        for token in ("emergency", "urgent", "trauma")
    ):
        return "EMER"
    if any(
        _contains_phrase(normalized_text, token)
        for token in ("observation", "obsenc", "observation unit")
    ):
        return "OBSENC"
    if any(
        _contains_phrase(normalized_text, token)
        for token in (
            "ambulatory",
            "outpatient",
            "same day",
            "same-day",
        )
    ):
        return "AMB"
    if any(
        _contains_phrase(normalized_text, token)
        for token in (
            "admit",
            "admitted",
            "admission",
            "inpatient",
            "elective",
        )
    ):
        return "IMP"
    return None


def _build_encounter_class(code: str) -> dict[str, str]:
    return {
        "system": _FHIR_ENCOUNTER_CLASS_SYSTEM,
        "code": code,
        "display": _FHIR_ENCOUNTER_CLASS_DISPLAY[code],
    }


def _normalize_encounter_status(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "finished"


def normalize_encounter_resource(
    resource: dict[str, Any], raw_payload: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return resource

    normalized = dict(resource)
    normalized["status"] = _normalize_encounter_status(normalized.get("status"))
    normalized.pop("related", None)

    current_class = normalized.get("class")
    if _looks_like_valid_encounter_class(current_class):
        return normalized

    admission = raw_payload.get("admission") if isinstance(raw_payload, dict) else None
    sql_evidence = raw_payload.get("sql_evidence") if isinstance(raw_payload, dict) else None
    encounter_rows: list[dict[str, Any]] = []
    if isinstance(sql_evidence, dict):
        raw_encounter_rows = sql_evidence.get("Encounter")
        if isinstance(raw_encounter_rows, list):
            encounter_rows = [
                row for row in raw_encounter_rows if isinstance(row, dict)
            ]
    encounter_sources = [
        value
        for value in (
            normalized.get("hospitalization"),
            normalized.get("type"),
            normalized.get("serviceType"),
            admission if isinstance(admission, dict) else {},
        )
        if isinstance(value, dict)
    ]
    encounter_sources.extend(encounter_rows)

    class_code = _derive_encounter_class_code_from_sources(encounter_sources)
    if class_code is None:
        normalized.pop("class", None)
        return normalized

    normalized["class"] = _build_encounter_class(class_code)
    return normalized
