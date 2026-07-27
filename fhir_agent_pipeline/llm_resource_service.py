from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .agents import FhirResourceGenerationAgent
from .config import Settings
from .fhir_schema import FhirSchemaRepository
from .json_utils import dump_json
from .resource_registry import resource_semantic_errors
from .source_contract import (
    encounter_reference,
    patient_reference,
    stable_resource_id,
)
from .vector_store import PgVectorKnowledgeStore


def empty_array_errors(value: Any, path: str = "resource") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            errors.extend(empty_array_errors(nested, f"{path}.{key}"))
    elif isinstance(value, list):
        if not value:
            errors.append(f"{path}는 빈 배열일 수 없습니다. 값이 없으면 제거하세요.")
        else:
            for index, nested in enumerate(value):
                errors.extend(empty_array_errors(nested, f"{path}[{index}]"))
    return errors


def _remove_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, nested in value.items()
            if (cleaned := _remove_empty(nested)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            cleaned
            for nested in value
            if (cleaned := _remove_empty(nested)) not in (None, "", [], {})
        ]
    return value


def _fhir_datetime(value: Any, timezone: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if "T" not in text:
        return text
    if parsed.tzinfo is None:
        return text + timezone
    return parsed.isoformat()


def _codeable(row: dict[str, Any]) -> dict[str, Any]:
    coding = {
        key: value
        for key, value in {
            "system": row.get("_code_system"),
            "code": (
                str(row["_code"]) if row.get("_code") is not None else None
            ),
        }.items()
        if value not in (None, "")
    }
    return {
        key: value
        for key, value in {
            "coding": [coding] if coding else None,
            "text": row.get("_display"),
        }.items()
        if value not in (None, "", [])
    }


def _number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        parsed = float(str(value))
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


@dataclass(frozen=True)
class FhirGenerationResult:
    resources: list[dict[str, Any]]
    llm_calls_used: int
    batches: int
    first_pass_valid_batches: int
    repaired_batches: int
    repair_attempts: int
    initial_error_count: int

    def metrics(self) -> dict[str, int | float]:
        failed_first_pass = self.batches - self.first_pass_valid_batches
        return {
            "batches": self.batches,
            "llm_calls_used": self.llm_calls_used,
            "first_pass_valid_batches": self.first_pass_valid_batches,
            "first_pass_valid_rate": (
                1.0 if not self.batches
                else self.first_pass_valid_batches / self.batches
            ),
            "repaired_batches": self.repaired_batches,
            "repair_success_rate": (
                1.0 if not failed_first_pass
                else self.repaired_batches / failed_first_pass
            ),
            "repair_attempts": self.repair_attempts,
            "initial_error_count": self.initial_error_count,
            "final_error_count": 0,
            "error_reduction_rate": 1.0,
        }


class FhirGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        resource_type: str,
        batch_index: int,
        llm_calls_used: int,
        validation_errors: list[str],
        previous_payload: Any,
    ) -> None:
        super().__init__(message)
        self.resource_type = resource_type
        self.batch_index = batch_index
        self.llm_calls_used = llm_calls_used
        self.validation_errors = validation_errors
        self.previous_payload = previous_payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "batch_index": self.batch_index,
            "llm_calls_used": self.llm_calls_used,
            "validation_errors": self.validation_errors,
            "previous_payload": self.previous_payload,
        }


class FhirLlmResourceService:
    """정책 mapping 없이 SQL 결과와 FHIR Schema로 리소스를 생성합니다."""

    def __init__(
        self,
        settings: Settings,
        schema: FhirSchemaRepository,
        agent: FhirResourceGenerationAgent,
        vector_store: PgVectorKnowledgeStore | None,
    ) -> None:
        self.settings = settings
        self.schema = schema
        self.agent = agent
        self.vector_store = vector_store

    def generate(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        patient_key: int | str,
    ) -> FhirGenerationResult:
        context = self._fhir_context(resource_type)
        resources: list[dict[str, Any]] = []
        calls = batches = first_pass = repaired = initial_errors = 0
        batch_size = self.settings.llm_fhir_batch_size
        for start in range(0, len(rows), batch_size):
            batches += 1
            batch = rows[start : start + batch_size]
            previous: Any = None
            errors: list[str] = []
            for attempt in range(1, self.settings.llm_fhir_max_attempts + 1):
                calls += 1
                try:
                    previous = self.agent.run(
                        resource_type,
                        batch,
                        self._identity_contract(resource_type, batch, patient_key),
                        context,
                        previous,
                        errors,
                    )
                    generated, errors = self._candidate_errors(
                        resource_type, batch, previous, patient_key
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    generated = []
                    errors = [f"{type(exc).__name__}: {exc}"]
                if not errors:
                    resources.extend(generated)
                    if attempt == 1:
                        first_pass += 1
                    else:
                        repaired += 1
                    break
                if attempt == 1:
                    initial_errors += len(errors)
                if attempt == self.settings.llm_fhir_max_attempts:
                    raise FhirGenerationError(
                        f"{resource_type} LLM 생성이 {attempt}회 후에도 실패했습니다: "
                        + "; ".join(errors[:10]),
                        resource_type=resource_type,
                        batch_index=batches - 1,
                        llm_calls_used=calls,
                        validation_errors=list(errors),
                        previous_payload=previous,
                    )
        return FhirGenerationResult(
            resources, calls, batches, first_pass, repaired,
            calls - batches, initial_errors,
        )

    def _fhir_context(self, resource_type: str) -> str:
        matches = (
            self.vector_store.search(
                self.settings.fhir_vector_table,
                f"FHIR R4 {resource_type} required properties references datatypes",
                k=self.settings.llm_fhir_context_search_k,
            )
            if self.settings.vectors_enabled and self.vector_store
            else []
        )
        if self.settings.vectors_enabled and not matches:
            raise RuntimeError(
                f"{self.settings.fhir_vector_table}에서 {resource_type} 문맥이 없습니다."
            )
        definition = self.schema.definition(resource_type)
        contract = {
            "resourceType": resource_type,
            "required": definition.get("required", []),
            "allowed": list(definition.get("properties", {})),
        }
        return (
            "Official local FHIR R4 JSON Schema:\n" + dump_json(contract)
            + (
                "\n\nVectorDB FHIR Schema retrieval:\n"
                + "\n\n".join(item["content"] for item in matches)
                if matches else ""
            )
        )

    def _identity_contract(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        patient_key: int | str,
    ) -> str:
        expected = [
            {
                "source_row": index,
                "resource_id": stable_resource_id(resource_type, row),
                "patient_reference": patient_reference(patient_key),
                "encounter_reference": (
                    encounter_reference(row["_encounter_key"])
                    if resource_type not in {"Patient", "Encounter"}
                    else None
                ),
            }
            for index, row in enumerate(rows)
        ]
        return dump_json({
            "rules": [
                "Return {resources:[{source_row:int,resource:object}]} only.",
                "Return exactly one FHIR resource per SQL row.",
                "Use only values present in the SQL row; never invent source values.",
                "Use a coding system only when the SQL _code_system/_unit_system provides it.",
                "Omit unavailable optional properties and empty arrays.",
                "Use FHIR R4 and the exact resource IDs/references below.",
            ],
            "default_timezone_for_naive_datetime": self.settings.fhir_default_timezone,
            "expected": expected,
        })

    def _candidate_errors(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        payload: Any,
        patient_key: int | str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
            return [], ["응답에 resources 배열이 없습니다."]
        wrappers = payload["resources"]
        errors: list[str] = []
        if len(wrappers) != len(rows):
            errors.append(
                f"리소스 수 {len(wrappers)}가 SQL 행 수 {len(rows)}와 다릅니다."
            )
        indexed: dict[int, dict[str, Any]] = {}
        for wrapper in wrappers:
            if not isinstance(wrapper, dict) or not isinstance(
                wrapper.get("source_row"), int
            ):
                errors.append("각 항목에는 정수 source_row가 필요합니다.")
                continue
            index = wrapper["source_row"]
            resource = wrapper.get("resource")
            if index in indexed:
                errors.append(f"source_row={index}가 중복됐습니다.")
            elif not isinstance(resource, dict):
                errors.append(f"source_row={index} resource가 객체가 아닙니다.")
            else:
                indexed[index] = resource
        generated: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            resource = indexed.get(index)
            if resource is None:
                errors.append(f"source_row={index}가 없습니다.")
                continue
            normalized = self._normalize(resource_type, row, resource, patient_key)
            generated.append(normalized)
            if normalized.get("resourceType") != resource_type:
                errors.append(f"source_row={index} resourceType 오류")
            if normalized.get("id") != stable_resource_id(resource_type, row):
                errors.append(f"source_row={index} id 오류")
            errors.extend(
                f"source_row={index} {item}"
                for item in (
                    self.schema.errors(normalized)
                    + resource_semantic_errors(normalized)
                    + empty_array_errors(normalized)
                )
            )
        return generated, errors

    def _normalize(
        self,
        resource_type: str,
        row: dict[str, Any],
        resource: dict[str, Any],
        patient_key: int | str,
    ) -> dict[str, Any]:
        normalized = dict(resource)
        normalized["resourceType"] = resource_type
        normalized["id"] = stable_resource_id(resource_type, row)
        normalized.pop("identifier", None)
        for unsupported in (
            "meta",
            "implicitRules",
            "language",
            "text",
            "contained",
            "extension",
            "modifierExtension",
        ):
            normalized.pop(unsupported, None)
        if resource_type == "Patient":
            for key in tuple(normalized):
                if key.startswith("deceased"):
                    normalized.pop(key, None)
            gender = str(row.get("_gender") or "").upper()
            normalized_gender = {
                "M": "male", "MALE": "male",
                "F": "female", "FEMALE": "female",
                "O": "other", "OTHER": "other",
                "U": "unknown", "UNKNOWN": "unknown",
            }.get(gender)
            if normalized_gender:
                normalized["gender"] = normalized_gender
            death = _fhir_datetime(
                row.get("_death_datetime"), self.settings.fhir_default_timezone
            )
            if death:
                normalized["deceasedDateTime"] = death
        if resource_type == "Encounter":
            normalized["subject"] = {"reference": patient_reference(patient_key)}
            encounter_type = row.get("_encounter_type")
            if encounter_type:
                normalized["type"] = [{"text": str(encounter_type)}]
            normalized["class"] = {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "IMP",
            }
            end = _fhir_datetime(
                row.get("_end_datetime"), self.settings.fhir_default_timezone
            )
            normalized["status"] = "finished" if end else "in-progress"
            normalized["period"] = _remove_empty({
                "start": _fhir_datetime(
                    row.get("_start_datetime"),
                    self.settings.fhir_default_timezone,
                ),
                "end": end,
            })
        elif resource_type not in {"Patient", "Encounter"}:
            normalized["subject"] = {"reference": patient_reference(patient_key)}
            normalized["encounter"] = {
                "reference": encounter_reference(row["_encounter_key"])
            }
            normalized["code"] = _codeable(row)
            if resource_type == "Procedure":
                for key in tuple(normalized):
                    if key.startswith("performed"):
                        normalized.pop(key, None)
                normalized["status"] = "completed"
                event_time = _fhir_datetime(
                    row.get("_event_datetime"),
                    self.settings.fhir_default_timezone,
                )
                if event_time:
                    normalized["performedDateTime"] = event_time
            if resource_type == "Observation":
                normalized["status"] = "final"
                for key in tuple(normalized):
                    if key.startswith(("effective", "value")):
                        normalized.pop(key, None)
                event_time = _fhir_datetime(
                    row.get("_event_datetime"),
                    self.settings.fhir_default_timezone,
                )
                if event_time:
                    normalized["effectiveDateTime"] = event_time
                if row.get("_value_number") is not None:
                    normalized["valueQuantity"] = _remove_empty({
                        "value": _number(row["_value_number"]),
                        "unit": row.get("_unit"),
                        "system": row.get("_unit_system"),
                        "code": row.get("_unit_code"),
                    })
                elif row.get("_value_text") is not None:
                    normalized["valueString"] = str(row["_value_text"])
                reference_range = _remove_empty({
                    "low": {
                        "value": _number(row.get("_reference_low")),
                    },
                    "high": {
                        "value": _number(row.get("_reference_high")),
                    },
                })
                if reference_range:
                    normalized["referenceRange"] = [reference_range]
                else:
                    normalized.pop("referenceRange", None)
        return _remove_empty(normalized)
