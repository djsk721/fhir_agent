from dataclasses import replace

import pytest

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.fhir_models import build_bundle_pages
from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.normalization import (
    normalize_administrative_gender,
)
from fhir_agent_pipeline.resource_definition import (
    definition_for,
    ordered_definitions,
)
from fhir_agent_pipeline.resource_registry import adapter_for
from fhir_agent_pipeline.resource_renderer import (
    DeterministicResourceRenderer,
    SourceRowContractError,
    empty_array_errors,
    fhir_datetime,
)


def _renderer() -> DeterministicResourceRenderer:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        vectors_enabled=False,
    )
    return DeterministicResourceRenderer(
        settings, get_fhir_schema(settings.fhir_schema_path)
    )


def test_observation_definition_exposes_deterministic_contract() -> None:
    definition = definition_for("Observation")
    payload = definition.as_dict()

    assert payload["generation_mode"] == "deterministic_renderer"
    assert "_effective_time" not in definition.source_aliases
    assert "_event_datetime" in definition.required_source_aliases
    assert "_encounter_key" in definition.optional_source_aliases
    assert payload["pagination_policy"] == {
        "enabled": True,
        "page_size": 500,
    }


def test_resource_registry_reuses_single_definition_source() -> None:
    definitions = ordered_definitions()

    assert [item.order for item in definitions] == sorted(
        item.order for item in definitions
    )
    assert all(
        adapter_for(item.resource_type) is item
        for item in definitions
    )


def test_definition_drives_query_limits_and_status_policy() -> None:
    settings = replace(
        load_settings(require_database=False),
        max_encounters=7,
        max_observations_per_encounter=11,
    )

    assert definition_for("Encounter").query_limit(settings, 5) == 7
    assert definition_for("Observation").query_limit(settings, 3) == 33
    assert definition_for("Encounter").status(closed=True) == "finished"
    assert definition_for("Encounter").status(closed=False) == "in-progress"
    assert definition_for("Observation").status() == "final"


def test_gender_normalization_is_shared_and_source_neutral() -> None:
    assert normalize_administrative_gender(" F ") == "female"
    assert normalize_administrative_gender("unknown") == "unknown"
    assert normalize_administrative_gender("not-mapped") is None


def test_renderer_creates_one_observation_per_source_row_without_llm() -> None:
    result = _renderer().generate(
        "Observation",
        [{
            "_patient_key": "P-1",
            "_encounter_key": "E-1",
            "_resource_key": "O-1",
            "_code": "LAB-1",
            "_event_datetime": "2026-07-28 12:00:00",
            "_code_system": None,
            "_display": "Example",
            "_value_number": "2.5",
            "_value_text": None,
            "_unit": "mg",
            "_unit_system": None,
            "_unit_code": None,
            "_reference_low": "1",
            "_reference_high": "3",
            "_interpretation": "normal",
        }],
        "P-1",
    )

    assert result.metrics()["llm_calls_used"] == 0
    assert len(result.resources) == 1
    observation = result.resources[0]
    assert observation["valueQuantity"]["value"] == 2.5
    assert observation["effectiveDateTime"] == "2026-07-28T12:00:00Z"
    assert observation["interpretation"] == [{"text": "normal"}]


def test_renderer_allows_patient_observation_without_encounter() -> None:
    row = {
        "_patient_key": "P-1",
        "_encounter_key": None,
        "_resource_key": "O-1",
        "_code": "LAB-1",
        "_event_datetime": "2026-07-28 12:00:00",
        "_code_system": None,
        "_display": None,
        "_value_number": None,
        "_value_text": "negative",
        "_unit": None,
        "_unit_system": None,
        "_unit_code": None,
        "_reference_low": None,
        "_reference_high": None,
        "_interpretation": None,
    }

    result = _renderer().generate(
        "Observation", [row], "P-1", encounter_keys=["E-1"]
    )

    assert result.resources[0]["valueString"] == "negative"
    assert "encounter" not in result.resources[0]


def test_renderer_omits_reference_to_encounter_outside_bundle() -> None:
    row = {
        "_patient_key": "P-1",
        "_encounter_key": "E-2",
        "_resource_key": "O-1",
        "_code": "LAB-1",
        "_event_datetime": "2026-07-28 12:00:00",
        "_code_system": None,
        "_display": None,
        "_value_number": "1",
        "_value_text": None,
        "_unit": None,
        "_unit_system": None,
        "_unit_code": None,
        "_reference_low": None,
        "_reference_high": None,
        "_interpretation": None,
    }

    result = _renderer().generate(
        "Observation", [row], "P-1", encounter_keys=["E-1"]
    )

    assert "encounter" not in result.resources[0]


def test_renderer_rejects_missing_required_alias_before_generation() -> None:
    with pytest.raises(SourceRowContractError, match="_event_datetime"):
        _renderer().generate(
            "Observation",
            [{
                "_patient_key": "P-1",
                "_encounter_key": "E-1",
                "_resource_key": "O-1",
                "_code": "LAB-1",
                "_code_system": None,
                "_display": None,
                "_value_number": None,
                "_value_text": None,
                "_unit": None,
                "_unit_system": None,
                "_unit_code": None,
                "_reference_low": None,
                "_reference_high": None,
                "_interpretation": None,
            }],
            "P-1",
        )


def test_renderer_rejects_clinical_row_outside_encounter_scope() -> None:
    row = {
        "_patient_key": "P-1",
        "_encounter_key": "E-2",
        "_resource_key": "C-1",
        "_code": "C",
        "_code_system": None,
        "_display": None,
    }
    with pytest.raises(SourceRowContractError, match="Encounter 범위"):
        _renderer().generate(
            "Condition", [row], "P-1", encounter_keys=["E-1"]
        )


def test_bundle_builder_pages_observations_deterministically() -> None:
    resources = [
        {"resourceType": "Patient", "id": "P-1"},
        *[
            {"resourceType": "Observation", "id": f"O-{index}"}
            for index in range(5)
        ],
    ]
    pages = build_bundle_pages(resources, observation_page_size=2)

    assert [len(page["entry"]) for page in pages] == [1, 2, 2, 1]


def test_renderer_helpers_normalize_datetime_and_reject_empty_arrays() -> None:
    assert fhir_datetime("2026-07-26", "Z") == "2026-07-26"
    assert (
        fhir_datetime("2026-07-26 12:34:56", "Z")
        == "2026-07-26T12:34:56Z"
    )
    assert empty_array_errors({"name": []})
