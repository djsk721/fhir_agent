from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mapping_config import MimicMappingConfig


@dataclass
class _Score:
    correct: int = 0
    total: int = 0

    def add(self, actual: Any, expected: Any) -> None:
        self.total += 1
        self.correct += _equivalent(actual, expected)

    def ratio(self) -> float:
        return 1.0 if not self.total else self.correct / self.total

    def as_dict(self) -> dict[str, int | float]:
        return {
            "correct": self.correct,
            "evaluated": self.total,
            "accuracy": self.ratio(),
        }


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(actual) == float(expected)
    if (
        isinstance(expected, str)
        and isinstance(actual, str)
        and len(expected) == 10
        and actual.startswith(expected + "T00:00:00")
    ):
        return True
    return actual == expected


def _path(value: Any, *parts: str | int) -> Any:
    current = value
    for part in parts:
        if isinstance(part, int):
            if not isinstance(current, list) or len(current) <= part:
                return None
            current = current[part]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
    return current


def _with_timezone(value: Any, timezone: str) -> Any:
    if not isinstance(value, str) or "T" not in value:
        return value
    suffix = value[10:]
    if suffix.endswith("Z") or "+" in suffix or "-" in suffix:
        return value
    return value + timezone


def _f1(expected: set[str], actual: set[str]) -> dict[str, int | float]:
    true_positive = len(expected & actual)
    false_positive = len(actual - expected)
    false_negative = len(expected - actual)
    precision = (
        1.0 if not actual else true_positive / (true_positive + false_positive)
    )
    recall = (
        1.0 if not expected else true_positive / (true_positive + false_negative)
    )
    f1 = (
        0.0
        if precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return {
        "expected": len(expected),
        "generated": len(actual),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _count_alignment(expected: int, generated: int) -> dict[str, int | float]:
    denominator = max(expected, generated)
    return {
        "expected": expected,
        "generated": generated,
        "difference": generated - expected,
        "alignment_rate": (
            1.0
            if denominator == 0
            else 1.0 - abs(generated - expected) / denominator
        ),
    }


def evaluate_task_output(
    mapping: MimicMappingConfig,
    source_rows: dict[str, list[dict[str, Any]]],
    generated_resources: dict[str, list[dict[str, Any]]],
    default_timezone: str,
) -> dict[str, Any]:
    """DB 조회 행과 LLM 리소스를 결정적 ID로 대응시켜 Task 품질을 계산합니다."""

    overall = _Score()
    per_type = {name: _Score() for name in source_rows}
    reference = _Score()
    subject_reference = _Score()
    encounter_reference = _Score()
    coding = _Score()
    icd_system = _Score()
    display = _Score()
    ucum = _Score()
    observation_value = _Score()
    observation_unit = _Score()
    observation_time = _Score()
    unsupported_fields: list[dict[str, str]] = []
    resource_f1: dict[str, dict[str, int | float]] = {}
    count_alignment: dict[str, dict[str, int | float]] = {}

    for resource_type, rows in source_rows.items():
        actual_items = generated_resources.get(resource_type, [])
        actual_by_id = {
            str(resource.get("id")): resource
            for resource in actual_items
            if resource.get("id") is not None
        }
        expected_ids = {
            mapping.resource_id_from_row(resource_type, row)
            for row in rows
        }
        actual_ids = set(actual_by_id)
        resource_f1[resource_type] = _f1(expected_ids, actual_ids)
        count_alignment[resource_type] = _count_alignment(
            len(rows), len(actual_items)
        )

        for resource in actual_items:
            for field in (
                set(resource)
                - mapping.allowed_top_level_for(resource_type)
            ):
                unsupported_fields.append(
                    {
                        "resource": f"{resource_type}/{resource.get('id', '?')}",
                        "field": field,
                    }
                )

        for row in rows:
            resource_id = mapping.resource_id_from_row(resource_type, row)
            resource = actual_by_id.get(resource_id)
            if resource is None:
                continue
            score = per_type[resource_type]

            def check(actual: Any, expected: Any) -> None:
                score.add(actual, expected)
                overall.add(actual, expected)

            check(resource.get("resourceType"), resource_type)
            check(resource.get("id"), resource_id)

            identifier_system = mapping.identifier_system_for(resource_type)
            check(_path(resource, "identifier", 0, "system"), identifier_system)
            check(_path(resource, "identifier", 0, "value"), resource_id)

            if resource_type == "Patient":
                gender = row.get(mapping.patient_columns.get("gender"))
                expected = mapping.patient_gender_map.get(str(gender or "").upper())
                if expected:
                    check(resource.get("gender"), expected)
                death_date = row.get(mapping.patient_columns.get("death_date"))
                check(
                    resource.get("deceasedDateTime"),
                    _with_timezone(death_date, default_timezone)
                    if death_date
                    else None,
                )
                continue

            subject_column = {
                "Encounter": mapping.admission_columns,
                "Condition": mapping.diagnosis_columns,
                "Procedure": mapping.procedure_columns,
                "Observation": mapping.lab_columns,
            }[resource_type].get("subject_id")
            patient_id = mapping.resource_id(
                "Patient", {"subject_id": row.get(subject_column)}
            )
            expected_subject = f"Patient/{patient_id}"
            actual_subject = _path(resource, "subject", "reference")
            check(actual_subject, expected_subject)
            reference.add(actual_subject, expected_subject)
            subject_reference.add(actual_subject, expected_subject)

            if resource_type == "Encounter":
                columns = mapping.admission_columns
                admission_type = str(
                    row.get(columns.get("admission_type")) or ""
                ).upper()
                check(
                    resource.get("status"),
                    mapping.encounter_status_when_ended
                    if row.get(columns.get("end_time"))
                    else mapping.encounter_status_when_open,
                )
                check(_path(resource, "class", "system"), mapping.encounter_class_system)
                check(
                    _path(resource, "class", "code"),
                    mapping.encounter_class_by_type.get(
                        admission_type, mapping.encounter_default_class
                    ),
                )
                check(
                    _path(resource, "period", "start"),
                    _with_timezone(row.get(columns.get("start_time")), default_timezone),
                )
                if row.get(columns.get("end_time")):
                    check(
                        _path(resource, "period", "end"),
                        _with_timezone(row.get(columns.get("end_time")), default_timezone),
                    )
                continue

            columns = mapping.columns_for(resource_type)
            admission_id = row.get(columns.get("admission_id"))
            expected_encounter = "Encounter/" + mapping.resource_id(
                "Encounter", {"admission_id": admission_id}
            )
            actual_encounter = _path(resource, "encounter", "reference")
            check(actual_encounter, expected_encounter)
            reference.add(actual_encounter, expected_encounter)
            encounter_reference.add(actual_encounter, expected_encounter)

            if resource_type in {"Condition", "Procedure"}:
                code = _path(resource, "code", "coding", 0)
                expected_code = str(row.get(columns.get("code")))
                expected_system = str(row.get("fhir_icd_system"))
                check(_path(code, "code"), expected_code)
                check(_path(code, "system"), expected_system)
                coding.add(_path(code, "code"), expected_code)
                coding.add(_path(code, "system"), expected_system)
                icd_system.add(_path(code, "system"), expected_system)
                source_display = row.get(columns.get("display"))
                if source_display:
                    check(_path(code, "display"), source_display)
                    display.add(_path(code, "display"), source_display)
                if resource_type == "Condition":
                    check(
                        _path(resource, "category", 0, "coding", 0, "system"),
                        mapping.fhir_default("condition_category_system"),
                    )
                    check(
                        _path(resource, "category", 0, "coding", 0, "code"),
                        mapping.fhir_default("condition_category_code"),
                    )
                if resource_type == "Procedure":
                    check(resource.get("status"), mapping.fhir_default("procedure_status"))
                    source_date = row.get(columns.get("date"))
                    if source_date:
                        check(resource.get("performedDateTime"), source_date)
                continue

            item_id = str(row.get(columns.get("item_id")))
            check(_path(resource, "code", "coding", 0, "system"), mapping.lab_item_system)
            check(_path(resource, "code", "coding", 0, "code"), item_id)
            coding.add(_path(resource, "code", "coding", 0, "system"), mapping.lab_item_system)
            coding.add(_path(resource, "code", "coding", 0, "code"), item_id)
            check(resource.get("status"), mapping.fhir_default("observation_status"))
            check(
                _path(resource, "category", 0, "coding", 0, "system"),
                mapping.fhir_default("observation_category_system"),
            )
            check(
                _path(resource, "category", 0, "coding", 0, "code"),
                mapping.fhir_default("observation_category_code"),
            )
            event_time = row.get(columns.get("event_time"))
            if event_time:
                expected_time = _with_timezone(event_time, default_timezone)
                actual_time = resource.get("effectiveDateTime")
                check(actual_time, expected_time)
                observation_time.add(actual_time, expected_time)
            numeric = row.get(columns.get("numeric_value"))
            text = row.get(columns.get("text_value"))
            if numeric is not None:
                actual_value = _path(resource, "valueQuantity", "value")
                check(actual_value, numeric)
                observation_value.add(actual_value, numeric)
            elif text is not None:
                actual_value = resource.get("valueString")
                expected_value = str(text)
                check(actual_value, expected_value)
                observation_value.add(actual_value, expected_value)
            source_unit = row.get(columns.get("unit"))
            if source_unit and numeric is not None:
                expected_ucum = mapping.ucum_code(source_unit)
                for actual, expected in (
                    (
                        _path(resource, "valueQuantity", "unit"),
                        str(source_unit),
                    ),
                    (
                        _path(resource, "valueQuantity", "system"),
                        mapping.fhir_default("ucum_system"),
                    ),
                    (
                        _path(resource, "valueQuantity", "code"),
                        expected_ucum,
                    ),
                ):
                    check(actual, expected)
                    observation_unit.add(actual, expected)
                ucum.add(
                    _path(resource, "valueQuantity", "code"), expected_ucum
                )
            for source_name, bound in (
                ("reference_low", "low"),
                ("reference_high", "high"),
            ):
                source_value = row.get(columns.get(source_name))
                if source_value is None:
                    continue
                check(
                    _path(resource, "referenceRange", 0, bound, "value"),
                    source_value,
                )
                if source_unit:
                    expected_ucum = mapping.ucum_code(source_unit)
                    check(
                        _path(resource, "referenceRange", 0, bound, "unit"),
                        str(source_unit),
                    )
                    check(
                        _path(resource, "referenceRange", 0, bound, "system"),
                        mapping.fhir_default("ucum_system"),
                    )
                    check(
                        _path(resource, "referenceRange", 0, bound, "code"),
                        expected_ucum,
                    )
                    ucum.add(
                        _path(resource, "referenceRange", 0, bound, "code"),
                        expected_ucum,
                    )
            source_interpretation = row.get(columns.get("interpretation"))
            if source_interpretation:
                check(
                    _path(resource, "interpretation", 0, "text"),
                    str(source_interpretation),
                )

    selected_f1 = [
        resource_f1[name]["f1"]
        for name in ("Condition", "Procedure", "Observation")
        if name in resource_f1
    ]
    generated_field_count = sum(
        len(resource)
        for resources in generated_resources.values()
        for resource in resources
    )
    expected_count = sum(len(rows) for rows in source_rows.values())
    generated_count = sum(
        len(resources) for resources in generated_resources.values()
    )
    macro_f1 = 1.0 if not selected_f1 else sum(selected_f1) / len(selected_f1)
    task_quality_pass = (
        overall.ratio() == 1.0
        and macro_f1 == 1.0
        and reference.ratio() == 1.0
        and coding.ratio() == 1.0
        and not unsupported_fields
    )
    return {
        "evaluation_basis": "LLM SQL source rows matched to LLM FHIR resources by configured deterministic ID",
        "task_quality_pass": task_quality_pass,
        "source_field_accuracy": overall.as_dict(),
        "resource_field_accuracy": {
            name: score.as_dict() for name, score in per_type.items()
        },
        "resource_identity_metrics": resource_f1,
        "resource_count_alignment": {
            "overall": _count_alignment(expected_count, generated_count),
            "by_resource": count_alignment,
        },
        "clinical_resource_macro_f1": macro_f1,
        "observation_quality": {
            "detection": resource_f1.get(
                "Observation", _f1(set(), set())
            ),
            "value_accuracy": observation_value.as_dict(),
            "unit_accuracy": observation_unit.as_dict(),
            "time_accuracy": observation_time.as_dict(),
        },
        "reference_accuracy": {
            "all": reference.as_dict(),
            "subject": subject_reference.as_dict(),
            "encounter": encounter_reference.as_dict(),
        },
        "terminology_accuracy": {
            "coding_system_and_code": coding.as_dict(),
            "icd_system": icd_system.as_dict(),
            "source_display": display.as_dict(),
            "ucum_code": ucum.as_dict(),
        },
        "unsupported_top_level_fields": {
            "count": len(unsupported_fields),
            "rate": (
                0.0
                if not generated_field_count
                else len(unsupported_fields) / generated_field_count
            ),
            "items": unsupported_fields[:100],
        },
    }
