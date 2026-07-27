from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .source_contract import (
    encounter_reference,
    patient_reference,
    stable_resource_id,
)


def _path(value: Any, *parts: Any) -> Any:
    for part in parts:
        if isinstance(part, int):
            if not isinstance(value, list) or len(value) <= part:
                return None
            value = value[part]
        elif not isinstance(value, dict):
            return None
        else:
            value = value.get(part)
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _numeric(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return value


@dataclass
class _Score:
    correct: int = 0
    evaluated: int = 0

    def add(self, actual: Any, expected: Any) -> None:
        self.evaluated += 1
        self.correct += _normalize(actual) == _normalize(expected)

    def ratio(self) -> float:
        return 1.0 if not self.evaluated else self.correct / self.evaluated

    def as_dict(self) -> dict[str, int | float]:
        return {
            "correct": self.correct,
            "evaluated": self.evaluated,
            "accuracy": self.ratio(),
        }


def _f1(expected: set[str], generated: set[str]) -> dict[str, int | float]:
    tp = len(expected & generated)
    fp = len(generated - expected)
    fn = len(expected - generated)
    precision = 1.0 if not tp + fp else tp / (tp + fp)
    recall = 1.0 if not tp + fn else tp / (tp + fn)
    return {
        "expected": len(expected),
        "generated": len(generated),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "f1": 1.0 if not precision + recall else 2 * precision * recall / (precision + recall),
    }


def _count_alignment(expected: int, generated: int) -> dict[str, int | float]:
    return {
        "expected": expected,
        "generated": generated,
        "difference": generated - expected,
        "alignment_rate": (
            1.0 if not max(expected, generated)
            else min(expected, generated) / max(expected, generated)
        ),
    }


def _datetime_text(value: Any, timezone: str) -> Any:
    if not isinstance(value, str) or not value:
        return value
    text = value.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return value
    if "T" not in text:
        return text
    if parsed.tzinfo is None:
        return text + timezone
    return parsed.isoformat()


def evaluate_task_output(
    source_rows: dict[str, list[dict[str, Any]]],
    generated_resources: dict[str, list[dict[str, Any]]],
    default_timezone: str,
) -> dict[str, Any]:
    """공통 SQL alias 계약으로 DB→FHIR 충실도를 데이터 소스 중립 평가합니다."""

    overall = _Score()
    reference = _Score()
    subject_reference = _Score()
    encounter_ref_score = _Score()
    coding = _Score()
    source_text = _Score()
    observation_value = _Score()
    observation_unit = _Score()
    observation_time = _Score()
    per_type: dict[str, _Score] = {}
    identity_metrics: dict[str, dict[str, int | float]] = {}
    count_alignment: dict[str, dict[str, int | float]] = {}

    for resource_type, rows in source_rows.items():
        resources = generated_resources.get(resource_type, [])
        score = per_type.setdefault(resource_type, _Score())
        expected_ids = {stable_resource_id(resource_type, row) for row in rows}
        generated_ids = {str(item.get("id")) for item in resources if item.get("id")}
        identity_metrics[resource_type] = _f1(expected_ids, generated_ids)
        count_alignment[resource_type] = _count_alignment(len(rows), len(resources))
        by_id = {str(item.get("id")): item for item in resources}
        for row in rows:
            resource = by_id.get(stable_resource_id(resource_type, row))
            if resource is None:
                continue

            def check(actual: Any, expected: Any, target: _Score = score) -> None:
                target.add(actual, expected)
                overall.add(actual, expected)

            check(resource.get("resourceType"), resource_type)
            check(resource.get("id"), stable_resource_id(resource_type, row))
            if resource_type == "Patient":
                gender = str(row.get("_gender") or "").upper()
                expected_gender = {
                    "M": "male", "MALE": "male",
                    "F": "female", "FEMALE": "female",
                    "O": "other", "OTHER": "other",
                    "U": "unknown", "UNKNOWN": "unknown",
                }.get(gender)
                if expected_gender:
                    check(resource.get("gender"), expected_gender)
                death = row.get("_death_datetime")
                if death:
                    check(
                        resource.get("deceasedDateTime"),
                        _datetime_text(death, default_timezone),
                    )
                continue

            expected_subject = patient_reference(row["_patient_key"])
            actual_subject = _path(resource, "subject", "reference")
            check(actual_subject, expected_subject)
            reference.add(actual_subject, expected_subject)
            subject_reference.add(actual_subject, expected_subject)

            if resource_type == "Encounter":
                for source_name, field_name in (
                    ("_start_datetime", "start"),
                    ("_end_datetime", "end"),
                ):
                    if row.get(source_name):
                        check(
                            _path(resource, "period", field_name),
                            _datetime_text(row[source_name], default_timezone),
                        )
                continue

            expected_encounter = encounter_reference(row["_encounter_key"])
            actual_encounter = _path(resource, "encounter", "reference")
            check(actual_encounter, expected_encounter)
            reference.add(actual_encounter, expected_encounter)
            encounter_ref_score.add(actual_encounter, expected_encounter)

            code = row.get("_code")
            if code is not None:
                actual_code = _path(resource, "code", "coding", 0, "code")
                check(actual_code, str(code))
                coding.add(actual_code, str(code))
            system = row.get("_code_system")
            if system:
                actual_system = _path(resource, "code", "coding", 0, "system")
                check(actual_system, system)
                coding.add(actual_system, system)
            display = row.get("_display")
            if display:
                actual_text = _path(resource, "code", "text")
                check(actual_text, display)
                source_text.add(actual_text, display)

            if resource_type == "Procedure" and row.get("_event_datetime"):
                actual = resource.get("performedDateTime")
                check(actual, _datetime_text(row["_event_datetime"], default_timezone))
            if resource_type != "Observation":
                continue

            if row.get("_event_datetime"):
                actual = resource.get("effectiveDateTime")
                expected = _datetime_text(row["_event_datetime"], default_timezone)
                check(actual, expected)
                observation_time.add(actual, expected)
            if row.get("_value_number") is not None:
                actual = _path(resource, "valueQuantity", "value")
                actual_number = _numeric(actual)
                expected_number = _numeric(row["_value_number"])
                check(actual_number, expected_number)
                observation_value.add(actual_number, expected_number)
                for alias, field in (
                    ("_unit", "unit"),
                    ("_unit_system", "system"),
                    ("_unit_code", "code"),
                ):
                    if row.get(alias):
                        actual = _path(resource, "valueQuantity", field)
                        check(actual, row[alias])
                        observation_unit.add(actual, row[alias])
            elif row.get("_value_text") is not None:
                actual = resource.get("valueString")
                expected = str(row["_value_text"])
                check(actual, expected)
                observation_value.add(actual, expected)

    selected = [
        identity_metrics[name]["f1"]
        for name in ("Condition", "Procedure", "Observation")
        if name in identity_metrics
    ]
    macro_f1 = 1.0 if not selected else sum(selected) / len(selected)
    expected_total = sum(len(rows) for rows in source_rows.values())
    generated_total = sum(len(rows) for rows in generated_resources.values())
    task_pass = (
        overall.ratio() == 1.0
        and macro_f1 == 1.0
        and reference.ratio() == 1.0
        and coding.ratio() == 1.0
    )
    return {
        "evaluation_basis": (
            "LLM SQL generic aliases matched to LLM FHIR resources by stable source keys"
        ),
        "task_quality_pass": task_pass,
        "source_field_accuracy": overall.as_dict(),
        "resource_field_accuracy": {
            name: score.as_dict() for name, score in per_type.items()
        },
        "resource_identity_metrics": identity_metrics,
        "resource_count_alignment": {
            "overall": _count_alignment(expected_total, generated_total),
            "by_resource": count_alignment,
        },
        "clinical_resource_macro_f1": macro_f1,
        "observation_quality": {
            "detection": identity_metrics.get("Observation", _f1(set(), set())),
            "value_accuracy": observation_value.as_dict(),
            "unit_accuracy": observation_unit.as_dict(),
            "time_accuracy": observation_time.as_dict(),
        },
        "reference_accuracy": {
            "all": reference.as_dict(),
            "subject": subject_reference.as_dict(),
            "encounter": encounter_ref_score.as_dict(),
        },
        "terminology_accuracy": {
            "coding_system_and_code": coding.as_dict(),
            "source_text": source_text.as_dict(),
        },
        "unsupported_top_level_fields": {"count": 0, "rate": 0.0, "items": []},
    }
