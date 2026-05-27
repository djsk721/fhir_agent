from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.requests import MimicPayload


@dataclass(frozen=True)
class ReferenceContext:
    subject_id: str | None
    hadm_ids: tuple[str, ...]
    itemid: str | None


def _extract_identifier_values(resource: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for identifier in resource.get("identifier", []):
        if not isinstance(identifier, dict):
            continue
        value = identifier.get("value")
        if value is not None:
            values.append(str(value))
    return values


def _extract_code_values(resource: dict[str, Any]) -> list[str]:
    code = resource.get("code")
    if not isinstance(code, dict):
        return []

    values: list[str] = []
    for coding in code.get("coding", []):
        if not isinstance(coding, dict):
            continue
        code_value = coding.get("code")
        if code_value is not None:
            values.append(str(code_value))
    return values


def _extract_reference(resource: dict[str, Any], field: str) -> str | None:
    reference = resource.get(field)
    if not isinstance(reference, dict):
        return None
    value = reference.get("reference")
    return str(value) if value is not None else None


def _extract_class_code(resource: dict[str, Any]) -> str | None:
    encounter_class = resource.get("class")
    if not isinstance(encounter_class, dict):
        return None
    code = encounter_class.get("code")
    return str(code) if code is not None else None


def _extract_quantity_value(resource: dict[str, Any]) -> str | None:
    value_quantity = resource.get("valueQuantity")
    if isinstance(value_quantity, dict) and value_quantity.get("value") is not None:
        return str(value_quantity.get("value"))
    value_string = resource.get("valueString")
    if value_string is not None:
        return str(value_string)
    return None


def _normalize_resources_by_type(
    generated_resources: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for resource_type, value in generated_resources.items():
        resource_group = value if isinstance(value, list) else [value]
        normalized[resource_type] = [
            resource for resource in resource_group if isinstance(resource, dict)
        ]
    return normalized


def _build_skipped_validation_result(
    normalized_resources: dict[str, list[dict[str, Any]]],
    fhir_dir: Path,
) -> dict[str, Any]:
    return {
        "summary": {
            "status": "skipped",
            "matched": 0,
            "mismatched": 0,
            "missing": 0,
            "skipped": sum(len(resources) for resources in normalized_resources.values()),
        },
        "items": [],
        "reference_directory": str(fhir_dir),
    }


def _reference_context(payload: MimicPayload) -> ReferenceContext:
    subject_id = payload.patient.get("subject_id")
    hadm_ids = [
        str(admission.get("hadm_id"))
        for admission in (payload.admissions or ([] if payload.admission is None else [payload.admission]))
        if isinstance(admission, dict) and admission.get("hadm_id") is not None
    ]
    itemid = payload.lab_event.get("itemid") if isinstance(payload.lab_event, dict) else None
    return ReferenceContext(
        subject_id=str(subject_id) if subject_id is not None else None,
        hadm_ids=tuple(hadm_ids),
        itemid=str(itemid) if itemid is not None else None,
    )


def _iter_ndjson_resources(file_path: Path):
    with file_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                resource = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(resource, dict):
                yield line_number, resource


def _load_reference_patient_ids(
    fhir_dir: Path,
    context: ReferenceContext,
) -> tuple[list[dict[str, Any]], list[str]]:
    if context.subject_id is None:
        return [], []

    matches: list[dict[str, Any]] = []
    patient_ids: list[str] = []
    for file_path in sorted(fhir_dir.glob("*Patient*.ndjson")):
        for line_number, resource in _iter_ndjson_resources(file_path):
            if resource.get("resourceType") != "Patient":
                continue
            if context.subject_id not in _extract_identifier_values(resource):
                continue
            matches.append(
                {
                    "file_name": file_path.name,
                    "line_number": line_number,
                    "resource": resource,
                }
            )
            resource_id = resource.get("id")
            if resource_id is not None:
                patient_ids.append(str(resource_id))

    return matches, patient_ids


def _build_difference(path: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "path": path,
        "expected": expected,
        "actual": actual,
    }


def _subject_reference_matches(
    generated_subject: str | None,
    reference_subject: str | None,
    context: ReferenceContext,
    patient_reference_ids: list[str],
) -> bool:
    if generated_subject == reference_subject:
        return True

    if generated_subject is None or context.subject_id is None:
        return False

    expected_generated_subject = f"Patient/{context.subject_id}"
    reference_subjects = {f"Patient/{patient_id}" for patient_id in patient_reference_ids}
    return generated_subject == expected_generated_subject and reference_subject in reference_subjects


def _compare_patient(
    generated_resource: dict[str, Any],
    reference_resource: dict[str, Any],
    context: ReferenceContext,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    generated_identifiers = _extract_identifier_values(generated_resource)
    reference_identifiers = _extract_identifier_values(reference_resource)

    if context.subject_id is not None and context.subject_id not in generated_identifiers:
        differences.append(
            _build_difference(
                "identifier[].value",
                context.subject_id,
                generated_identifiers,
            )
        )
    if context.subject_id is not None and context.subject_id not in reference_identifiers:
        differences.append(
            _build_difference(
                "reference.identifier[].value",
                context.subject_id,
                reference_identifiers,
            )
        )
    if generated_resource.get("gender") != reference_resource.get("gender"):
        differences.append(
            _build_difference(
                "gender",
                reference_resource.get("gender"),
                generated_resource.get("gender"),
            )
        )
    if generated_resource.get("birthDate") != reference_resource.get("birthDate"):
        differences.append(
            _build_difference(
                "birthDate",
                reference_resource.get("birthDate"),
                generated_resource.get("birthDate"),
            )
        )

    return differences


def _compare_encounter(
    generated_resource: dict[str, Any],
    reference_resource: dict[str, Any],
    context: ReferenceContext,
    patient_reference_ids: list[str],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if _extract_class_code(generated_resource) != _extract_class_code(reference_resource):
        differences.append(
            _build_difference(
                "class.code",
                _extract_class_code(reference_resource),
                _extract_class_code(generated_resource),
            )
        )

    generated_subject = _extract_reference(generated_resource, "subject")
    reference_subject = _extract_reference(reference_resource, "subject")
    if not _subject_reference_matches(
        generated_subject,
        reference_subject,
        context,
        patient_reference_ids,
    ):
        differences.append(
            _build_difference("subject.reference", reference_subject, generated_subject)
        )

    generated_period = generated_resource.get("period")
    reference_period = reference_resource.get("period")
    if generated_period != reference_period:
        differences.append(
            _build_difference("period", reference_period, generated_period)
        )

    return differences


def _compare_observation(
    generated_resource: dict[str, Any],
    reference_resource: dict[str, Any],
    context: ReferenceContext,
    patient_reference_ids: list[str],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    generated_codes = sorted(_extract_code_values(generated_resource))
    reference_codes = sorted(_extract_code_values(reference_resource))
    if generated_codes != reference_codes:
        differences.append(
            _build_difference("code.coding[].code", reference_codes, generated_codes)
        )

    generated_subject = _extract_reference(generated_resource, "subject")
    reference_subject = _extract_reference(reference_resource, "subject")
    if not _subject_reference_matches(
        generated_subject,
        reference_subject,
        context,
        patient_reference_ids,
    ):
        differences.append(
            _build_difference("subject.reference", reference_subject, generated_subject)
        )

    if generated_resource.get("effectiveDateTime") != reference_resource.get(
        "effectiveDateTime"
    ):
        differences.append(
            _build_difference(
                "effectiveDateTime",
                reference_resource.get("effectiveDateTime"),
                generated_resource.get("effectiveDateTime"),
            )
        )

    if _extract_quantity_value(generated_resource) != _extract_quantity_value(reference_resource):
        differences.append(
            _build_difference(
                "value",
                _extract_quantity_value(reference_resource),
                _extract_quantity_value(generated_resource),
            )
        )

    return differences


def _compare_resource(
    resource_type: str,
    generated_resource: dict[str, Any],
    reference_resource: dict[str, Any],
    context: ReferenceContext,
    patient_reference_ids: list[str],
) -> list[dict[str, Any]]:
    if resource_type == "Patient":
        return _compare_patient(generated_resource, reference_resource, context)
    if resource_type == "Encounter":
        return _compare_encounter(
            generated_resource,
            reference_resource,
            context,
            patient_reference_ids,
        )
    if resource_type == "Observation":
        return _compare_observation(
            generated_resource,
            reference_resource,
            context,
            patient_reference_ids,
        )
    return []


def _observation_candidate_signature(
    resource: dict[str, Any],
) -> tuple[tuple[str, ...], str | None, str | None]:
    return (
        tuple(sorted(_extract_code_values(resource))),
        resource.get("effectiveDateTime") if resource.get("effectiveDateTime") is None else str(resource.get("effectiveDateTime")),
        _extract_quantity_value(resource),
    )


def _find_reference_candidate(
    resource_type: str,
    generated_resource: dict[str, Any],
    context: ReferenceContext,
    patient_reference_ids: list[str],
    patient_candidates: list[dict[str, Any]],
    fhir_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    if resource_type == "Patient":
        if len(patient_candidates) == 1:
            return patient_candidates[0], None
        if len(patient_candidates) > 1:
            return None, "Multiple reference Patient resources matched the same subject_id."
        return None, "No reference Patient resource matched the input subject_id."

    if resource_type == "Encounter":
        generated_id = generated_resource.get("id")
        if generated_id is None:
            return None, "Generated Encounter has no id to match against hadm_id."
        reference_subjects = {f"Patient/{patient_id}" for patient_id in patient_reference_ids}
        matches: list[dict[str, Any]] = []
        for file_path in sorted(fhir_dir.glob("*Encounter*.ndjson")):
            for line_number, resource in _iter_ndjson_resources(file_path):
                if resource.get("resourceType") != "Encounter":
                    continue
                if str(generated_id) not in _extract_identifier_values(resource):
                    continue
                if reference_subjects:
                    subject_reference = _extract_reference(resource, "subject")
                    if subject_reference not in reference_subjects:
                        continue
                matches.append(
                    {
                        "file_name": file_path.name,
                        "line_number": line_number,
                        "resource": resource,
                    }
                )
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "Multiple reference Encounter resources matched the same hadm_id."
        return None, "No reference Encounter resource matched the generated encounter id/hadm_id."

    if resource_type == "Observation":
        generated_codes = set(_extract_code_values(generated_resource))
        if not generated_codes:
            return None, "Generated Observation has no coded item to compare."
        reference_subjects = {f"Patient/{patient_id}" for patient_id in patient_reference_ids}
        matches: list[dict[str, Any]] = []
        for file_path in sorted(fhir_dir.glob("*Observation*.ndjson")):
            for line_number, resource in _iter_ndjson_resources(file_path):
                if resource.get("resourceType") != "Observation":
                    continue
                resource_codes = set(_extract_code_values(resource))
                if not generated_codes.intersection(resource_codes):
                    continue
                if reference_subjects:
                    subject_reference = _extract_reference(resource, "subject")
                    if subject_reference not in reference_subjects:
                        continue
                matches.append(
                    {
                        "file_name": file_path.name,
                        "line_number": line_number,
                        "resource": resource,
                    }
                )
        if len(matches) > 1:
            generated_signature = _observation_candidate_signature(generated_resource)
            narrowed_matches = [
                match
                for match in matches
                if _observation_candidate_signature(match["resource"]) == generated_signature
            ]
            if len(narrowed_matches) == 1:
                return narrowed_matches[0], None
            if len(narrowed_matches) > 1:
                matches = narrowed_matches
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "Multiple reference Observation resources matched the generated code for the same patient."
        return None, "No unique reference Observation resource matched the generated code for the patient."

    return None, f"Cross-validation is not implemented for resource type '{resource_type}'."


class CrossValidationService:
    def __init__(self, fhir_dir: str | Path = "fhir") -> None:
        self._fhir_dir = Path(fhir_dir)

    def validate(
        self,
        payload: MimicPayload,
        generated_resources: dict[str, Any],
        enabled: bool = True,
    ) -> dict[str, Any]:
        normalized_resources = _normalize_resources_by_type(generated_resources)
        if not enabled:
            return _build_skipped_validation_result(normalized_resources, self._fhir_dir)

        if not self._fhir_dir.exists():
            return _build_skipped_validation_result(normalized_resources, self._fhir_dir)

        context = _reference_context(payload)
        patient_candidates, patient_reference_ids = _load_reference_patient_ids(
            self._fhir_dir,
            context,
        )

        matched = 0
        mismatched = 0
        missing = 0
        skipped = 0
        items: list[dict[str, Any]] = []

        for resource_type, resources in normalized_resources.items():
            for generated_resource in resources:
                candidate, skipped_reason = _find_reference_candidate(
                    resource_type,
                    generated_resource,
                    context,
                    patient_reference_ids,
                    patient_candidates,
                    self._fhir_dir,
                )

                if candidate is None:
                    status = "missing"
                    if skipped_reason and skipped_reason.startswith("Multiple"):
                        status = "skipped"
                        skipped += 1
                    else:
                        missing += 1
                    items.append(
                        {
                            "resource_type": resource_type,
                            "generated_id": generated_resource.get("id"),
                            "generated_resource": generated_resource,
                            "reference_file": candidate["file_name"] if candidate else None,
                            "reference_line": candidate["line_number"] if candidate else None,
                            "reference_resource": candidate["resource"] if candidate else None,
                            "status": status,
                            "differences": [],
                            "skipped_reason": skipped_reason,
                        }
                    )
                    continue

                differences = _compare_resource(
                    resource_type,
                    generated_resource,
                    candidate["resource"],
                    context,
                    patient_reference_ids,
                )
                status = "matched" if not differences else "mismatched"
                if status == "matched":
                    matched += 1
                else:
                    mismatched += 1
                items.append(
                    {
                        "resource_type": resource_type,
                        "generated_id": generated_resource.get("id"),
                        "generated_resource": generated_resource,
                        "reference_file": candidate["file_name"],
                        "reference_line": candidate["line_number"],
                        "reference_resource": candidate["resource"],
                        "status": status,
                        "differences": differences,
                        "skipped_reason": None,
                    }
                )

        if mismatched > 0 or missing > 0:
            overall_status = "failed"
        elif matched > 0:
            overall_status = "passed"
        else:
            overall_status = "skipped"

        return {
            "summary": {
                "status": overall_status,
                "matched": matched,
                "mismatched": mismatched,
                "missing": missing,
                "skipped": skipped,
            },
            "items": items,
            "reference_directory": str(self._fhir_dir),
        }
