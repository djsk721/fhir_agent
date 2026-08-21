from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Settings
from .fhir_schema import FhirSchemaRepository
from .normalization import normalize_administrative_gender
from .resource_definition import ResourceDefinition, definition_for
from .resource_registry import resource_semantic_errors
from .source_contract import (
    encounter_reference,
    patient_reference,
    stable_resource_id,
)


class SourceRowContractError(ValueError):
    pass


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


def remove_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, nested in value.items()
            if (cleaned := remove_empty(nested)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            cleaned
            for nested in value
            if (cleaned := remove_empty(nested)) not in (None, "", [], {})
        ]
    return value


def fhir_datetime(value: Any, timezone: str) -> str | None:
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


def codeable_concept(row: dict[str, Any]) -> dict[str, Any]:
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


def source_number(value: Any) -> int | float | None:
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
class RenderResult:
    resources: list[dict[str, Any]]
    rows_rendered: int

    def metrics(self) -> dict[str, int | float]:
        return {
            "source_rows": self.rows_rendered,
            "resources_rendered": len(self.resources),
            "count_alignment_rate": (
                1.0
                if self.rows_rendered == len(self.resources)
                else 0.0
            ),
            "llm_calls_used": 0,
        }


def validate_source_rows(
    definition: ResourceDefinition,
    rows: list[dict[str, Any]],
    patient_key: int | str,
    encounter_keys: list[int | str] | None = None,
) -> None:
    """Renderer 진입 전에 alias, scope, identity와 필수값을 검증합니다."""

    allowed_encounters = (
        {str(value) for value in encounter_keys}
        if encounter_keys is not None
        else None
    )
    resource_keys: list[str] = []
    errors: list[str] = []
    expected_aliases = set(definition.source_aliases)
    for index, row in enumerate(rows):
        missing = expected_aliases - set(row)
        if missing:
            errors.append(f"row[{index}] alias 누락: {sorted(missing)}")
        empty_required = [
            alias
            for alias in definition.required_source_aliases
            if row.get(alias) in (None, "")
        ]
        if empty_required:
            errors.append(
                f"row[{index}] 필수 alias 값 누락: {sorted(empty_required)}"
            )
        if str(row.get("_patient_key")) != str(patient_key):
            errors.append(f"row[{index}]가 요청 환자 범위를 벗어났습니다.")
        if definition.has_resource_identity:
            resource_keys.append(str(row.get("_resource_key")))
            if (
                definition.requires_encounter_scope
                and
                allowed_encounters is not None
                and str(row.get("_encounter_key")) not in allowed_encounters
            ):
                errors.append(
                    f"row[{index}]가 선택 Encounter 범위를 벗어났습니다."
                )
    if len(resource_keys) != len(set(resource_keys)):
        errors.append("_resource_key가 중복됐습니다.")
    if errors:
        raise SourceRowContractError(
            f"{definition.resource_type} Source Row Contract 위반: "
            + "; ".join(errors[:20])
        )


@dataclass(frozen=True)
class ReferenceContext:
    patient_key: int | str
    encounter_keys: frozenset[str] | None = None

    def includes_encounter(self, encounter_key: Any) -> bool:
        return (
            encounter_key not in (None, "")
            and (
                self.encounter_keys is None
                or str(encounter_key) in self.encounter_keys
            )
        )


class DeterministicResourceRenderer:
    """Resource Definition에 따라 source row를 LLM 호출 없이 변환합니다."""

    def __init__(
        self,
        settings: Settings,
        schema: FhirSchemaRepository,
    ) -> None:
        self.settings = settings
        self.schema = schema

    def generate(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        patient_key: int | str,
        encounter_keys: list[int | str] | None = None,
    ) -> RenderResult:
        definition = definition_for(resource_type)
        validate_source_rows(
            definition, rows, patient_key, encounter_keys
        )
        context = ReferenceContext(
            patient_key,
            (
                frozenset(str(value) for value in encounter_keys)
                if encounter_keys is not None
                else None
            ),
        )
        resources = [
            self.render(row, context, definition) for row in rows
        ]
        errors = [
            f"row[{index}] {error}"
            for index, resource in enumerate(resources)
            for error in (
                self.schema.errors(resource)
                + resource_semantic_errors(resource)
                + empty_array_errors(resource)
            )
        ]
        if errors:
            raise ValueError(
                f"{resource_type} 결정적 Renderer 검증 실패: "
                + "; ".join(errors[:20])
            )
        return RenderResult(resources=resources, rows_rendered=len(rows))

    def render(
        self,
        row: dict[str, Any],
        context: ReferenceContext,
        definition: ResourceDefinition,
    ) -> dict[str, Any]:
        resource_type = definition.resource_type
        resource: dict[str, Any] = {
            "resourceType": resource_type,
            "id": stable_resource_id(resource_type, row),
        }
        if resource_type == "Patient":
            normalized_gender = normalize_administrative_gender(
                row.get("_gender")
            )
            if normalized_gender:
                resource["gender"] = normalized_gender
            death = self._datetime(row.get("_death_datetime"))
            if death:
                resource["deceasedDateTime"] = death
        elif resource_type == "Encounter":
            end = self._datetime(row.get("_end_datetime"))
            resource.update({
                "status": definition.status(closed=bool(end)),
                "class": {"code": str(row["_encounter_type"])},
                "subject": {
                    "reference": patient_reference(context.patient_key)
                },
                "period": remove_empty({
                    "start": self._datetime(row.get("_start_datetime")),
                    "end": end,
                }),
            })
            if row.get("_encounter_type"):
                resource["type"] = [{"text": str(row["_encounter_type"])}]
        else:
            resource.update({
                "subject": {
                    "reference": patient_reference(context.patient_key)
                },
                "code": codeable_concept(row),
            })
            if context.includes_encounter(row.get("_encounter_key")):
                resource["encounter"] = {
                    "reference": encounter_reference(row["_encounter_key"])
                }
            if resource_type == "Procedure":
                resource["status"] = definition.status()
                if event_time := self._datetime(row.get("_event_datetime")):
                    resource["performedDateTime"] = event_time
            elif resource_type == "Observation":
                resource["status"] = definition.status()
                resource["effectiveDateTime"] = self._datetime(
                    row.get("_event_datetime")
                )
                if row.get("_value_number") is not None:
                    resource["valueQuantity"] = remove_empty({
                        "value": source_number(row["_value_number"]),
                        "unit": row.get("_unit"),
                        "system": row.get("_unit_system"),
                        "code": row.get("_unit_code"),
                    })
                elif row.get("_value_text") is not None:
                    resource["valueString"] = str(row["_value_text"])
                reference_range = remove_empty({
                    "low": {"value": source_number(row.get("_reference_low"))},
                    "high": {"value": source_number(row.get("_reference_high"))},
                })
                if reference_range:
                    resource["referenceRange"] = [reference_range]
                if row.get("_interpretation"):
                    resource["interpretation"] = [{
                        "text": str(row["_interpretation"])
                    }]
        return remove_empty(resource)

    def _datetime(self, value: Any) -> str | None:
        return fhir_datetime(value, self.settings.fhir_default_timezone)
