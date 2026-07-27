from __future__ import annotations

import hashlib
import re
from typing import Any


FHIR_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "Patient": ("_patient_key", "_gender", "_death_datetime"),
    "Encounter": (
        "_patient_key",
        "_encounter_key",
        "_encounter_type",
        "_start_datetime",
        "_end_datetime",
    ),
    "Condition": (
        "_patient_key",
        "_encounter_key",
        "_resource_key",
        "_code",
        "_code_system",
        "_display",
    ),
    "Procedure": (
        "_patient_key",
        "_encounter_key",
        "_resource_key",
        "_code",
        "_code_system",
        "_display",
        "_event_datetime",
    ),
    "Observation": (
        "_patient_key",
        "_encounter_key",
        "_resource_key",
        "_code",
        "_code_system",
        "_display",
        "_event_datetime",
        "_value_number",
        "_value_text",
        "_unit",
        "_unit_system",
        "_unit_code",
        "_reference_low",
        "_reference_high",
        "_interpretation",
    ),
}

_FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


def stable_resource_id(resource_type: str, row: dict[str, Any]) -> str:
    key_name = {
        "Patient": "_patient_key",
        "Encounter": "_encounter_key",
    }.get(resource_type, "_resource_key")
    raw = str(row.get(key_name, "")).strip()
    if not raw:
        raise ValueError(f"{resource_type} SQL 결과에 {key_name} 값이 없습니다.")
    if _FHIR_ID.fullmatch(raw):
        return raw
    digest = hashlib.sha256(
        f"{resource_type}:{raw}".encode("utf-8")
    ).hexdigest()[:32]
    return f"src-{digest}"


def patient_reference(patient_key: Any) -> str:
    return "Patient/" + stable_resource_id(
        "Patient", {"_patient_key": patient_key}
    )


def encounter_reference(encounter_key: Any) -> str:
    return "Encounter/" + stable_resource_id(
        "Encounter", {"_encounter_key": encounter_key}
    )
