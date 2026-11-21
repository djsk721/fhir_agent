from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re

import pytest

from research.fhir_profile.profiles.schema import bind_profile_identity, canonical_identity, validate_profile
from research.fhir_profile.profiles.output_schema import profile_generation_schema
from research.fhir_profile.agents.mapping import select_profile_examples
from research.fhir_profile.renderer.operations import (
    OMIT, apply_mapping, apply_part, render_records, render_records_detailed, resource_id,
    validate_record_identity,
)
from research.fhir_profile.validation.fidelity import evaluate_references


def profile(mappings: list[dict], target: str = "Observation") -> dict:
    return {
        "profile_id": "independent-source",
        "profile_version": "1",
        "target_resource": target,
        "fhir_version": "4.0.1",
        "source": {"dataset": "external", "table": "events"},
        "identity": {"source_fields": ["event_id"]},
        "mappings": mappings,
    }


def test_full_fhir_target_path_is_checked_instead_of_a_root_allowlist() -> None:
    valid = profile([
        {"id": "note", "operation": "string", "source_field": "note", "target_path": "note.0.text"},
    ])
    assert validate_profile(valid, {"columns": ["event_id", "note"]}) == valid
    invalid = deepcopy(valid)
    invalid["mappings"][0]["target_path"] = "note.0.madeUpField"
    with pytest.raises(ValueError, match="target_path"):
        validate_profile(invalid)
    invalid["mappings"][0]["target_path"] = "note.text"
    with pytest.raises(ValueError, match="target_path"):
        validate_profile(invalid)


def test_source_fields_in_nested_parts_conditions_and_reference_identity_are_checked() -> None:
    mappings = [
        {"id": "code", "target_path": "code", "operation": "codeable_concept", "code": {"source_field": "missing"}},
        {"id": "value", "target_path": "valueString", "operation": "string", "source_field": "value", "when": {"field": "missing", "present": True}},
        {"id": "subject", "target_path": "subject", "operation": "reference", "resource_type": "Patient", "identity": {"source_fields": ["missing"]}},
    ]
    for mapping in mappings:
        with pytest.raises(ValueError, match="unknown source field"):
            validate_profile(profile([mapping]), {"columns": ["event_id", "value"]})


def test_synthea_numeric_and_text_choices_have_exclusive_safe_conditions() -> None:
    value_profile = profile([
        {"id": "numeric", "target_path": "valueQuantity.value", "operation": "decimal", "source_field": "VALUE", "when": {"field": "TYPE", "equals": "numeric"}},
        {"id": "text", "target_path": "valueString", "operation": "copy", "source_field": "VALUE", "when": {"field": "TYPE", "in": ["text"]}},
    ])
    resources, errors = render_records(value_profile, [
        {"event_id": "one", "TYPE": "numeric", "VALUE": "12.25"},
        {"event_id": "two", "TYPE": "text", "VALUE": "positive"},
    ])
    assert errors == []
    assert resources[0]["valueQuantity"]["value"] == 12.25
    assert "valueString" not in resources[0]
    assert resources[1]["valueString"] == "positive"
    assert "valueQuantity" not in resources[1]
    ambiguous = deepcopy(value_profile)
    ambiguous["mappings"][1].pop("when")
    with pytest.raises(ValueError, match="choice"):
        validate_profile(ambiguous)


def test_source_examples_represent_declared_result_types_without_exceeding_budget() -> None:
    rows = ({"kind": "decimal", "value": "1.2"}, {"kind": "decimal", "value": "1.3"},
            {"kind": "decimal", "value": "1.4"}, {"kind": "comment", "value": "retained"})
    assert select_profile_examples(rows, 2, ["kind"]) == [rows[0], rows[3]]
    assert select_profile_examples(rows, 3, ["kind"]) == [rows[0], rows[3], rows[1]]
    assert select_profile_examples(rows, 2, []) == list(rows[:2])
    assert select_profile_examples(rows, 0, ["kind"]) == []


def test_source_structure_rejects_line_locator_and_incompatible_fk_encoding() -> None:
    patient = profile([
        {"id": "gender", "target_path": "gender", "operation": "string", "source_field": "sex"},
    ], "Patient")
    declared_patient_columns = {"columns": ["business_key", "event_id", "sex"], "identity_fields": ["business_key"]}
    generation = profile_generation_schema("Patient", declared_patient_columns)
    identity_fields = generation["properties"]["identity"]["properties"]["source_fields"]
    assert identity_fields["items"]["enum"] == ["business_key"]
    with pytest.raises(ValueError, match="원천 업무키"):
        validate_profile(patient, declared_patient_columns)
    patient["identity"]["source_fields"] = ["business_key"]
    assert validate_profile(patient, declared_patient_columns) == patient

    metadata = {
        "columns": ["event_id", "fk", "other"],
        "foreign_keys": [{"columns": ["fk"], "referred_table": "people", "referred_columns": ["business_key"]}],
        "related_sources": {"Patient": {"table": "people", "identity_fields": ["business_key"]}},
    }
    event = profile([
        {"id": "ref", "operation": "reference", "resource_type": "Patient", "target_path": "subject", "source_field": "other"},
    ])
    with pytest.raises(ValueError, match="원천 외래 키"):
        validate_profile(event, metadata)
    event["mappings"][0]["source_field"] = "fk"
    resources, errors = render_records(event, [{"event_id": 1, "fk": "k-1"}], metadata=metadata)
    assert not errors
    assert resources[0]["subject"]["reference"] == "Patient/k-1"


def test_managed_id_comes_from_contract_and_cannot_be_used_as_clinical_input() -> None:
    metadata = {"columns": ["candidate_fk", "value"], "record_identity_fields": ["_stable_row_key"],
                "identity_contract": True}
    draft = profile([
        {"id": "value", "target_path": "valueString", "operation": "string", "source_field": "value"}
    ])
    draft["identity"] = {"source_fields": ["candidate_fk"]}
    with pytest.raises(ValueError, match="원천 업무키"):
        validate_profile(draft, metadata)
    locked = bind_profile_identity(draft, metadata)
    assert draft["identity"]["source_fields"] == ["candidate_fk"]
    assert locked["identity"] == {"source_fields": ["_stable_row_key"]}
    rows = [{"_stable_row_key": "input.csv:2", "candidate_fk": "", "value": "unchanged"}]
    result = render_records_detailed(locked, rows, metadata=metadata)
    assert result.source_indices == [0]
    assert result.resources[0]["valueString"] == "unchanged"
    assert result.resources[0]["id"] == resource_id(locked, rows[0])
    assert re.fullmatch(r"[a-f0-9]{64}", result.resources[0]["id"])
    locked["mappings"][0]["source_field"] = "_stable_row_key"
    with pytest.raises(ValueError, match="unknown source field"):
        validate_profile(locked, metadata)
    assert canonical_identity({**metadata, "identity_fields": ["value"]}) == {"source_fields": ["value"]}
    with pytest.raises(ValueError, match="관리형 identity"):
        canonical_identity({"identity_contract": True})


def test_managed_id_preflight_rejects_missing_or_duplicate_keys_before_render() -> None:
    metadata = {"columns": ["value"], "record_identity_fields": ["_stable_row_key"], "identity_contract": True}
    locked = bind_profile_identity(profile([
        {"id": "value", "target_path": "valueQuantity.value", "operation": "decimal", "source_field": "value"}
    ]), metadata)
    with pytest.raises(ValueError, match="원천 값이 없습니다"):
        validate_record_identity([{"_stable_row_key": "   "}], metadata)
    with pytest.raises(ValueError, match="스칼라"):
        validate_record_identity([{"_stable_row_key": float("nan")}], metadata)
    with pytest.raises(ValueError, match=r"중복.*record\[0\].*record\[1\]"):
        render_records_detailed(locked, [
            {"_stable_row_key": 1, "value": "invalid decimal"},
            {"_stable_row_key": "1", "value": 3},
        ], metadata=metadata)


def test_declared_optional_reference_omits_only_missing_keys_and_preserves_unresolved_reference() -> None:
    metadata = {"columns": ["visit_fk", "value"], "record_identity_fields": ["_stable_row_key"],
                "identity_contract": True, "column_nullable": {"visit_fk": True},
                "related_sources": {"Encounter": {"table": "visits", "identity_fields": ["visit_id"]}},
                "foreign_keys": [{"columns": ["visit_fk"], "referred_table": "visits", "referred_columns": ["visit_id"]}]}
    managed = bind_profile_identity(profile([
        {"id": "ref", "target_path": "encounter", "operation": "reference", "resource_type": "Encounter",
         "source_field": "visit_fk", "null_policy": "error"},
        {"id": "value", "target_path": "valueString", "operation": "string", "source_field": "value"},
    ]), metadata)
    normalized = validate_profile(managed, metadata)
    assert normalized["mappings"][0]["null_policy"] == "omit"
    assert managed["mappings"][0]["null_policy"] == "error"
    result = render_records_detailed(managed, [
        {"_stable_row_key": "row1", "visit_fk": "", "value": "negative"},
        {"_stable_row_key": "row2", "visit_fk": "not-listed", "value": "positive"},
    ], metadata=metadata)
    assert result.errors == [] and result.source_indices == [0, 1]
    assert "encounter" not in result.resources[0]
    assert result.resources[1]["encounter"]["reference"] == "Encounter/not-listed"
    assert evaluate_references(result.resources, [])["accuracy"] == 0
    malformed = {"id": "visit", "operation": "reference", "resource_type": "Encounter",
                 "identity": {"source_fields": ["site", "visit"]}}
    with pytest.raises(ValueError, match="복합 참조"):
        apply_mapping(malformed, {"site": "location", "visit": ""})


def test_managed_reference_nullability_falls_back_to_target_schema(tmp_path) -> None:
    import json
    from research.fhir_profile.profiles.schema import DEFAULT_SCHEMA_PATH

    metadata = {"columns": ["visit_fk"], "record_identity_fields": ["_stable_row_key"], "identity_contract": True,
                "related_sources": {"Encounter": {"table": "visits", "identity_fields": ["visit_id"]}},
                "foreign_keys": [{"columns": ["visit_fk"], "referred_table": "visits", "referred_columns": ["visit_id"]}]}
    managed = bind_profile_identity(profile([
        {"id": "ref", "target_path": "encounter", "operation": "reference", "resource_type": "Encounter",
         "source_field": "visit_fk", "null_policy": "error"},
    ]), metadata)
    row = {"_stable_row_key": "row", "visit_fk": None}
    assert render_records_detailed(managed, [row], metadata=metadata).source_indices == [0]
    nonnullable = {**metadata, "column_nullable": {"visit_fk": False}}
    result = render_records_detailed(managed, [row], metadata=nonnullable)
    assert not result.resources and "참조 원천 값이 없습니다" in result.errors[0]

    required_schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_schema["definitions"]["Observation"].setdefault("required", []).append("encounter")
    path = tmp_path / "required-schema.json"
    path.write_text(json.dumps(required_schema), encoding="utf-8")
    normalized = validate_profile(managed, {**metadata, "column_nullable": {"visit_fk": True}}, schema_path=path)
    with pytest.raises(ValueError, match="참조 원천 값이 없습니다"):
        apply_mapping(normalized["mappings"][0], row)


def test_missing_numeric_value_can_select_a_text_branch_without_eval() -> None:
    value_profile = profile([
        {"id": "numeric", "target_path": "valueQuantity.value", "operation": "decimal", "source_field": "number", "when": {"field": "number", "present": True}},
        {"id": "text", "target_path": "valueString", "operation": "string", "source_field": "text", "when": {"field": "number", "present": False}},
    ])
    resources, errors = render_records(value_profile, [{"event_id": 1, "number": None, "text": "negative"}])
    assert not errors
    assert resources[0]["valueString"] == "negative"
    value_profile["mappings"][0]["when"] = {"eval": "__import__('os')"}
    with pytest.raises(ValueError, match="SQL"):
        validate_profile(value_profile)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", Decimal("NaN")])
def test_decimal_rejects_nonfinite_values(value: object) -> None:
    with pytest.raises(ValueError, match="유한|범위"):
        apply_part({"operation": "decimal", "source_field": "value"}, {"value": value})


def test_copy_preserves_json_types_and_boolean_is_explicit() -> None:
    original = {"value": {"flags": [True, False], "amount": 3}}
    copied = apply_part({"operation": "copy", "source_field": "value"}, original)
    assert copied == original["value"]
    assert copied is not original["value"]
    assert apply_part({"operation": "boolean", "source_field": "value"}, {"value": "false"}) is False
    with pytest.raises(ValueError, match="boolean"):
        apply_part({"operation": "boolean", "source_field": "value"}, {"value": "yes"})
    with pytest.raises(ValueError, match="integer"):
        apply_part({"operation": "integer", "source_field": "value"}, {"value": "1.2"})


def test_datetime_preserves_fractional_seconds_and_requires_timezone_policy() -> None:
    part = {"operation": "datetime", "source_field": "time"}
    for timestamp in ("2022-03-04T12:34:56.123456789Z", "2022-03-04T12:34:56.123400+09:00", "2022-03"):
        assert apply_part(part, {"time": timestamp}) == timestamp
    with pytest.raises(ValueError, match="timezone"):
        apply_part(part, {"time": "2022-03-04 12:34:56.123"})
    assert apply_part({**part, "timezone": "+09:00"}, {"time": "2022-03-04 12:34:56.123"}) == "2022-03-04T12:34:56.123+09:00"


def test_uuid_composite_and_long_ids_are_stable_and_references_use_the_same_rules() -> None:
    patient_profile = profile([], "Patient")
    uuid = "6e44cb1b-c18e-4c73-9cb9-a26bb2d3706d"
    assert resource_id(patient_profile, {"event_id": uuid}) == uuid
    patient_profile["identity"] = {"source_fields": ["site", "patient"], "separator": "-"}
    record = {"site": "a-b", "patient": "c", "event_id": 1}
    identity = resource_id(patient_profile, record)
    assert re.fullmatch(r"[a-f0-9]{64}", identity)
    assert identity != resource_id(patient_profile, {"site": "a", "patient": "b-c"})
    mapping = {"id": "subject", "operation": "reference", "resource_type": "Patient", "identity": patient_profile["identity"]}
    assert apply_mapping(mapping, record) == {"reference": f"Patient/{identity}"}
    assert len(resource_id(profile([]), {"event_id": "x" * 100})) == 64


def test_render_failures_keep_source_alignment_outside_fhir_and_do_not_invent_status() -> None:
    number_profile = profile([
        {"id": "value", "target_path": "valueQuantity.value", "operation": "decimal", "source_field": "value"},
    ])
    result = render_records_detailed(number_profile, [
        {"event_id": "bad", "value": "not-a-number"},
        {"event_id": "good", "value": 4},
    ])
    assert result.source_indices == [1]
    assert len(result.errors) == 1
    assert "mapping value (valueQuantity.value)" in result.errors[0]
    assert result.resources == [{"resourceType": "Observation", "id": "good", "valueQuantity": {"value": 4}}]
    assert apply_mapping({"operation": "string", "source_field": "value", "when": {"field": "value", "present": False}}, {"value": 0}) is OMIT
