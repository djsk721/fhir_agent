from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class HumanName(BaseModel):
    """FHIR `HumanName`의 최소 구조를 표현합니다."""

    family: str | None = None
    given: list[str] | None = None


class Reference(BaseModel):
    """FHIR `Reference`의 최소 구조를 표현합니다."""

    reference: str


class Coding(BaseModel):
    """FHIR `Coding`의 최소 구조를 표현합니다."""

    system: str | None = None
    code: str
    display: str | None = None


class CodeableConcept(BaseModel):
    """FHIR `CodeableConcept`의 최소 구조를 표현합니다."""

    coding: list[Coding] | None = None
    text: str | None = None


class Period(BaseModel):
    """FHIR `Period`의 최소 구조를 표현합니다."""

    start: str | None = None
    end: str | None = None


class EncounterClass(BaseModel):
    """FHIR `Encounter.class`의 최소 구조를 표현합니다."""

    system: str | None = None
    code: str
    display: str | None = None


class PatientResource(BaseModel):
    """FHIR R4 Patient 리소스의 경량 검증 모델입니다."""

    model_config = ConfigDict(extra="allow")

    resourceType: Literal["Patient"]
    id: str | None = None
    name: list[HumanName] | None = None
    gender: Literal["male", "female", "other", "unknown"] | None = None
    birthDate: date | None = None


class EncounterResource(BaseModel):
    """FHIR R4 Encounter 리소스의 경량 검증 모델입니다."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    resourceType: Literal["Encounter"]
    id: str | None = None
    status: str
    class_fhir: EncounterClass = Field(alias="class")
    subject: Reference
    period: Period | None = None


class ObservationResource(BaseModel):
    """FHIR R4 Observation 리소스의 경량 검증 모델입니다."""

    model_config = ConfigDict(extra="allow")

    resourceType: Literal["Observation"]
    id: str | None = None
    status: str
    code: CodeableConcept
    subject: Reference
    encounter: Reference | None = None
    effectiveDateTime: str | None = None
    valueString: str | None = None
    valueQuantity: dict[str, Any] | None = None


class BundleEntry(BaseModel):
    """FHIR `Bundle.entry`의 최소 구조를 표현합니다."""

    fullUrl: str | None = None
    resource: dict[str, Any]


class BundleResource(BaseModel):
    """FHIR R4 Bundle 리소스의 경량 검증 모델입니다."""

    model_config = ConfigDict(extra="allow")

    resourceType: Literal["Bundle"]
    type: str
    entry: list[BundleEntry]


def validate_fhir_json(resource: dict[str, Any], resource_type: str) -> tuple[bool, list[str]]:
    """FHIR JSON의 최소 구조와 필수 필드를 검증합니다."""

    if not isinstance(resource, dict):
        return False, ["FHIR JSON must be an object."]

    errors: list[str] = []
    actual_resource_type = resource.get("resourceType")
    if actual_resource_type != resource_type:
        errors.append(
            f"resourceType must be '{resource_type}', got '{actual_resource_type}'."
        )
        return False, errors

    if resource_type == "Patient":
        try:
            PatientResource.model_validate(resource)
        except ValidationError as exc:
            for item in exc.errors():
                location = ".".join(str(part) for part in item.get("loc", []))
                message = item.get("msg", "validation error")
                errors.append(f"{location}: {message}" if location else str(message))

    elif resource_type == "Encounter":
        try:
            EncounterResource.model_validate(resource)
        except ValidationError as exc:
            for item in exc.errors():
                location = ".".join(str(part) for part in item.get("loc", []))
                message = item.get("msg", "validation error")
                errors.append(f"{location}: {message}" if location else str(message))

    elif resource_type == "Observation":
        try:
            ObservationResource.model_validate(resource)
        except ValidationError as exc:
            for item in exc.errors():
                location = ".".join(str(part) for part in item.get("loc", []))
                message = item.get("msg", "validation error")
                errors.append(f"{location}: {message}" if location else str(message))

    return (len(errors) == 0), errors


def build_bundle(resources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """생성된 리소스들을 FHIR Bundle로 묶습니다."""

    entries: list[dict[str, Any]] = []
    for resource_type, resource in resources.items():
        resource_id = resource.get("id", resource_type.lower())
        entries.append(
            {
                "fullUrl": f"urn:resource:{resource_type.lower()}:{resource_id}",
                "resource": resource,
            }
        )

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": entries,
    }


def validate_bundle(bundle: dict[str, Any], required_resource_types: list[str]) -> tuple[bool, list[str]]:
    """최종 Bundle 구조와 포함 리소스를 검증합니다."""

    errors: list[str] = []
    try:
        BundleResource.model_validate(bundle)
    except ValidationError as exc:
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", []))
            message = item.get("msg", "validation error")
            errors.append(f"{location}: {message}" if location else str(message))
        return False, errors

    actual_types = [
        entry.get("resource", {}).get("resourceType")
        for entry in bundle.get("entry", [])
        if isinstance(entry, dict)
    ]
    for required_type in required_resource_types:
        if required_type not in actual_types:
            errors.append(f"Bundle is missing required resourceType '{required_type}'.")

    return (len(errors) == 0), errors
