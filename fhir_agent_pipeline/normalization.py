from __future__ import annotations

from typing import Any


FHIR_ADMINISTRATIVE_GENDER = {
    "M": "male",
    "MALE": "male",
    "F": "female",
    "FEMALE": "female",
    "O": "other",
    "OTHER": "other",
    "U": "unknown",
    "UNKNOWN": "unknown",
}


def normalize_administrative_gender(value: Any) -> str | None:
    normalized = str(value or "").strip().upper()
    return FHIR_ADMINISTRATIVE_GENDER.get(normalized)
