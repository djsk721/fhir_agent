from __future__ import annotations

from typing import Any, Protocol


class SupportsAdmissionLookup(Protocol):
    def fetch_admissions_for_patient(
        self, subject_id: int | str | None
    ) -> list[dict[str, Any]]: ...


def extract_admissions(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_admissions = raw_payload.get("admissions")
    if isinstance(raw_admissions, list):
        admissions = [admission for admission in raw_admissions if isinstance(admission, dict)]
        if admissions:
            return admissions

    admission = raw_payload.get("admission")
    if isinstance(admission, dict):
        return [admission]

    return []


def normalize_payload_shape(raw_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw_payload)
    admissions = extract_admissions(raw_payload)
    normalized["admissions"] = admissions or None
    normalized["admission"] = admissions[0] if admissions else None
    return normalized


def canonical_subject_id(raw_payload: dict[str, Any]) -> int | str | None:
    patient = raw_payload.get("patient")
    if isinstance(patient, dict):
        return patient.get("subject_id")
    return None


def canonical_patient_id(raw_payload: dict[str, Any]) -> str | None:
    subject_id = canonical_subject_id(raw_payload)
    if subject_id is None:
        return None
    return str(subject_id)


def _filter_admissions_for_subject(
    admissions: list[dict[str, Any]],
    subject_id: int | str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if subject_id is None:
        return admissions, []

    filtered_admissions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for admission in admissions:
        admission_subject_id = admission.get("subject_id")
        if admission_subject_id is None or str(admission_subject_id) == str(subject_id):
            filtered_admissions.append(admission)
        else:
            warnings.append(
                "Discarded admission data inconsistent with the input subject_id."
            )

    return filtered_admissions, warnings


def _normalize_lab_event_for_subject(
    raw_payload: dict[str, Any],
    canonical_subject_id: int | str | None,
    canonical_hadm_ids: set[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    lab_event = raw_payload.get("lab_event")
    if not isinstance(lab_event, dict):
        return lab_event if isinstance(lab_event, dict) else None, []

    warnings: list[str] = []
    normalized_lab_event = dict(lab_event)
    event_subject_id = normalized_lab_event.get("subject_id")
    if (
        canonical_subject_id is not None
        and event_subject_id is not None
        and str(event_subject_id) != str(canonical_subject_id)
    ):
        warnings.append("Discarded lab_event data inconsistent with the input subject_id.")
        return None, warnings

    hadm_id = normalized_lab_event.get("hadm_id")
    if hadm_id is not None and canonical_hadm_ids and str(hadm_id) not in canonical_hadm_ids:
        normalized_lab_event.pop("hadm_id", None)
        warnings.append("Removed lab_event.hadm_id inconsistent with the input subject_id.")

    return normalized_lab_event, warnings


def prepare_payload_for_subject(
    raw_payload: dict[str, Any],
    sql_runtime: SupportsAdmissionLookup | None = None,
) -> tuple[dict[str, Any], list[str]]:
    normalized_payload = normalize_payload_shape(raw_payload)
    warnings: list[str] = []
    subject_id = canonical_subject_id(normalized_payload)

    if sql_runtime is not None and subject_id is not None:
        try:
            fetched_admissions = sql_runtime.fetch_admissions_for_patient(subject_id)
        except Exception:
            fetched_admissions = []
        existing_admissions = extract_admissions(normalized_payload)
        if fetched_admissions:
            normalized_payload["admissions"] = _merge_admissions(existing_admissions, fetched_admissions)
            normalized_payload["admission"] = normalized_payload["admissions"][0] if normalized_payload["admissions"] else None
        elif not existing_admissions:
            warnings.append("No data found for given subject_id")

    filtered_admissions, admission_warnings = _filter_admissions_for_subject(
        extract_admissions(normalized_payload),
        subject_id,
    )
    warnings.extend(admission_warnings)
    normalized_payload["admissions"] = filtered_admissions or None
    normalized_payload["admission"] = filtered_admissions[0] if filtered_admissions else None

    canonical_hadm_ids = {
        str(admission.get("hadm_id"))
        for admission in filtered_admissions
        if admission.get("hadm_id") is not None
    }
    normalized_lab_event, lab_event_warnings = _normalize_lab_event_for_subject(
        normalized_payload,
        subject_id,
        canonical_hadm_ids,
    )
    warnings.extend(lab_event_warnings)
    normalized_payload["lab_event"] = normalized_lab_event

    return normalized_payload, warnings


def _merge_admissions(
    existing_admissions: list[dict[str, Any]], fetched_admissions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged_by_key: dict[str, dict[str, Any]] = {}

    for admission in existing_admissions:
        key = _admission_identity_key(admission)
        merged_by_key[key] = dict(admission)

    for admission in fetched_admissions:
        key = _admission_identity_key(admission)
        if key in merged_by_key:
            merged_by_key[key] = {
                **merged_by_key[key],
                **admission,
            }
        else:
            merged_by_key[key] = dict(admission)

    merged = list(merged_by_key.values())
    merged.sort(key=_admission_sort_key)
    return merged


def _admission_identity_key(admission: dict[str, Any]) -> str:
    hadm_id = admission.get("hadm_id")
    if hadm_id is not None:
        return f"hadm:{hadm_id}"

    subject_id = admission.get("subject_id")
    admittime = admission.get("admittime")
    return f"fallback:{subject_id}:{admittime}"


def _admission_sort_key(admission: dict[str, Any]) -> tuple[str, str]:
    admittime = str(admission.get("admittime") or "")
    hadm_id = str(admission.get("hadm_id") or "")
    return admittime, hadm_id
