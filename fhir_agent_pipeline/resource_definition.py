from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_PAGE_SIZE = 500


@dataclass(frozen=True)
class ResourceDefinition:
    """FHIR 대상, source-row 계약, 실행 순서와 검증 정책의 단일 정의."""

    resource_type: str
    order: int
    required_source_aliases: tuple[str, ...]
    optional_source_aliases: tuple[str, ...]
    fhir_mapping_policy: dict[str, str]
    dependencies: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    source_required: bool = False
    bundle_required: bool = False
    identity_alias: str = "_resource_key"
    encounter_scoped: bool = False
    query_limit_setting: str | None = None
    query_limit_per_encounter: bool = False
    page_size: int = DEFAULT_PAGE_SIZE
    pagination_enabled: bool = False
    fixed_status: str | None = None
    closed_status: str | None = None
    open_status: str | None = None

    @property
    def source_aliases(self) -> tuple[str, ...]:
        return self.required_source_aliases + self.optional_source_aliases

    @property
    def has_resource_identity(self) -> bool:
        return self.identity_alias == "_resource_key"

    @property
    def requires_encounter_scope(self) -> bool:
        return self.encounter_scoped

    def query_limit(self, settings: Any, encounter_count: int) -> int:
        if self.query_limit_setting is None:
            return 1
        configured = int(getattr(settings, self.query_limit_setting))
        return (
            configured * max(1, encounter_count)
            if self.query_limit_per_encounter
            else configured
        )

    def status(self, *, closed: bool | None = None) -> str | None:
        if self.fixed_status is not None:
            return self.fixed_status
        if closed is None:
            return None
        return self.closed_status if closed else self.open_status

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generation_mode"] = "deterministic_renderer"
        payload["pagination_policy"] = {
            "enabled": self.pagination_enabled,
            "page_size": self.page_size,
        }
        payload["validation_policy"] = {
            "count_alignment": True,
            "source_value_accuracy": True,
            "reference_resolution": True,
            "fhir_schema_validation": True,
        }
        return payload


RESOURCE_DEFINITIONS: dict[str, ResourceDefinition] = {
    "Patient": ResourceDefinition(
        resource_type="Patient",
        order=0,
        required_source_aliases=("_patient_key",),
        optional_source_aliases=("_gender", "_death_datetime"),
        fhir_mapping_policy={
            "id": "stable id from _patient_key",
            "gender": "normalized from _gender when present",
            "deceasedDateTime": "from _death_datetime when present",
        },
        required_fields=("id",),
        source_required=True,
        bundle_required=True,
        identity_alias="_patient_key",
    ),
    "Encounter": ResourceDefinition(
        resource_type="Encounter",
        order=10,
        required_source_aliases=(
            "_patient_key",
            "_encounter_key",
            "_encounter_type",
        ),
        optional_source_aliases=("_start_datetime", "_end_datetime"),
        fhir_mapping_policy={
            "subject": "Patient reference from _patient_key",
            "status": "closed_status when ended, otherwise open_status",
            "class": "source Coding code from _encounter_type",
            "period": "from _start_datetime and _end_datetime",
            "type": "text from _encounter_type when present",
        },
        dependencies=("Patient",),
        required_fields=("id", "status", "class", "subject"),
        source_required=True,
        bundle_required=True,
        identity_alias="_encounter_key",
        query_limit_setting="max_encounters",
        closed_status="finished",
        open_status="in-progress",
    ),
    "Condition": ResourceDefinition(
        resource_type="Condition",
        order=20,
        required_source_aliases=(
            "_patient_key",
            "_encounter_key",
            "_resource_key",
            "_code",
        ),
        optional_source_aliases=("_code_system", "_display"),
        fhir_mapping_policy={
            "subject": "Patient reference from _patient_key",
            "encounter": "Encounter reference from _encounter_key",
            "code": "CodeableConcept from _code, _code_system and _display",
        },
        dependencies=("Patient", "Encounter"),
        required_fields=("id", "code", "subject"),
        encounter_scoped=True,
        query_limit_setting="max_conditions_per_encounter",
        query_limit_per_encounter=True,
    ),
    "Procedure": ResourceDefinition(
        resource_type="Procedure",
        order=30,
        required_source_aliases=(
            "_patient_key",
            "_encounter_key",
            "_resource_key",
            "_code",
        ),
        optional_source_aliases=(
            "_code_system",
            "_display",
            "_event_datetime",
        ),
        fhir_mapping_policy={
            "status": "fixed_status",
            "subject": "Patient reference from _patient_key",
            "encounter": "Encounter reference from _encounter_key",
            "code": "CodeableConcept from _code, _code_system and _display",
            "performedDateTime": "from _event_datetime when present",
        },
        dependencies=("Patient", "Encounter"),
        required_fields=("id", "status", "code", "subject"),
        encounter_scoped=True,
        query_limit_setting="max_procedures_per_encounter",
        query_limit_per_encounter=True,
        fixed_status="completed",
    ),
    "Observation": ResourceDefinition(
        resource_type="Observation",
        order=40,
        required_source_aliases=(
            "_patient_key",
            "_resource_key",
            "_code",
            "_event_datetime",
        ),
        optional_source_aliases=(
            "_encounter_key",
            "_code_system",
            "_display",
            "_value_number",
            "_value_text",
            "_unit",
            "_unit_system",
            "_unit_code",
            "_reference_low",
            "_reference_high",
            "_interpretation",
        ),
        fhir_mapping_policy={
            "status": "fixed_status",
            "subject": "Patient reference from _patient_key",
            "encounter": (
                "Encounter reference only when _encounter_key identifies an "
                "Encounter included in the generated output"
            ),
            "code": "CodeableConcept from _code, _code_system and _display",
            "effectiveDateTime": "from _event_datetime",
            "valueQuantity": "when _value_number exists",
            "valueString": "when _value_text exists and _value_number is absent",
            "referenceRange": "from _reference_low and _reference_high",
            "interpretation": "text from _interpretation when present",
        },
        dependencies=("Patient", "Encounter"),
        required_fields=("id", "status", "code", "subject"),
        query_limit_setting="max_observations_per_encounter",
        query_limit_per_encounter=True,
        pagination_enabled=True,
        fixed_status="final",
    ),
}


def definition_for(resource_type: str) -> ResourceDefinition:
    try:
        return RESOURCE_DEFINITIONS[resource_type]
    except KeyError as exc:
        raise ValueError(
            f"지원하지 않는 resourceType입니다: {resource_type!r}"
        ) from exc


def ordered_definitions(
    resource_types: list[str] | None = None,
) -> list[ResourceDefinition]:
    selected = (
        set(RESOURCE_DEFINITIONS)
        if resource_types is None
        else set(resource_types)
    )
    return sorted(
        (
            definition
            for definition in RESOURCE_DEFINITIONS.values()
            if definition.resource_type in selected
        ),
        key=lambda definition: definition.order,
    )


def supported_resource_types() -> set[str]:
    return set(RESOURCE_DEFINITIONS)
