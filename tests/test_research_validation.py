from __future__ import annotations


def test_reference_targets_exist_but_encounter_belongs_to_another_patient():
    from research.fhir_profile.validation.fidelity import evaluate_references
    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Patient", "id": "p2"},
        {"resourceType": "Encounter", "id": "e2", "subject": {"reference": "Patient/p2"}},
        {"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e2"}},
    ]
    result = evaluate_references(resources, resources)
    assert result["accuracy"] < 1
    assert any(item["category"] == "reference_wrong_patient" for item in result["issues"])

from copy import deepcopy

from research.fhir_profile.contracts import SourceBatch
from research.fhir_profile.correction.policy import revise_profile
from research.fhir_profile.pipeline import HybridFhirPipeline, mapping_accuracy
from research.fhir_profile.validation.fidelity import (
    evaluate_fidelity,
    evaluate_reference_resources,
    evaluate_references,
    project_comparison_resources,
)


def patient_profile() -> dict:
    return {
        "profile_id": "patients-test", "profile_version": "1", "target_resource": "Patient",
        "fhir_version": "4.0.1", "source": {"dataset": "synthetic", "table": "patients"},
        "identity": {"source_fields": ["patient_id"]},
        "mappings": [{"id": "gender", "target_path": "gender", "operation": "copy", "source_field": "sex"}],
    }


def patient_batch(sex: str = "male") -> SourceBatch:
    return SourceBatch("synthetic", "1", "patients", "1", ({"patient_id": "p1", "sex": sex},), {"columns": ["patient_id", "sex"]})


def test_profile_consistency_does_not_claim_independent_fidelity() -> None:
    result = HybridFhirPipeline().run(patient_batch(), patient_profile())
    assert result["profile_execution_consistency"]["source_field_accuracy"]["accuracy"] == 1.0
    assert result["fidelity"]["status"] == "not_evaluated"
    assert result["fidelity"]["source_field_accuracy"]["accuracy"] is None


def test_wrong_but_consistently_executed_profile_fails_independent_gold() -> None:
    gold = [{"resourceType": "Patient", "id": "p1", "gender": "female"}]
    result = HybridFhirPipeline().run(patient_batch(), patient_profile(), expected_resources=gold)
    assert result["profile_execution_consistency"]["source_field_accuracy"]["accuracy"] == 1.0
    assert result["fidelity"]["source_field_accuracy"]["accuracy"] < 1.0
    assert result["metrics"]["standard_valid"] is True
    assert result["metrics"]["final_valid"] is False


def test_actual_json_schema_is_used_and_invalid_gender_blocks() -> None:
    result = HybridFhirPipeline().run(patient_batch("man"), patient_profile())
    assert result["validation"]["json_schema_used"] is True
    assert any(issue["category"] == "json_schema" for issue in result["issues"])
    assert result["metrics"]["final_valid"] is False


def test_slash_reference_is_not_proof_of_resolution() -> None:
    resources = [{"resourceType": "Observation", "id": "o1", "subject": {"reference": "Patient/absent"}}]
    unknown = evaluate_references(resources)
    assert unknown["status"] == "not_evaluated"
    assert unknown["accuracy"] is None
    checked = evaluate_references(resources, [{"resourceType": "Patient", "id": "p1"}])
    assert checked["accuracy"] == 0
    assert checked["issues"][0]["category"] == "reference_unresolved"
    checked = evaluate_references(resources, [{"resourceType": "Patient", "id": "absent"}])
    assert checked["accuracy"] == 1


def test_disjoint_events_have_zero_f1() -> None:
    report = evaluate_reference_resources(
        [{"resourceType": "Patient", "id": "wrong"}],
        [{"resourceType": "Patient", "id": "expected"}],
    )
    assert report["event_detection"]["precision"] == 0
    assert report["event_detection"]["recall"] == 0
    assert report["event_detection"]["f1"] == 0
    assert report["event_match_coverage"]["accuracy"] == 0
    assert report["matched_event_field_accuracy"]["status"] == "not_evaluated"
    assert report["matched_event_field_accuracy"]["accuracy"] is None


def test_field_accuracy_reports_conditional_accuracy_and_end_to_end_loss_separately() -> None:
    expected = [
        {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "2000-01-01"},
        {"resourceType": "Patient", "id": "p2", "gender": "female", "birthDate": "2001-01-01"},
    ]
    generated = [{"resourceType": "Patient", "id": "p1", "gender": "male"}]
    report = evaluate_reference_resources(
        generated, expected, field_paths={"Patient": ["gender", "birthDate"]},
    )
    assert report["event_match_coverage"] == {
        "status": "evaluated", "correct": 1, "evaluated": 2, "accuracy": 0.5,
    }
    assert report["matched_event_field_accuracy"] == {
        "status": "evaluated", "correct": 1, "evaluated": 2, "accuracy": 0.5,
    }
    assert report["source_field_accuracy"] == {
        "status": "evaluated", "correct": 1, "evaluated": 4, "accuracy": 0.25,
    }


def test_expected_reference_metrics_separate_missing_references_and_wrong_targets() -> None:
    patients = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Patient", "id": "p2"},
    ]
    encounters = [
        {"resourceType": "Encounter", "id": encounter_id, "subject": {"reference": "Patient/p1"}}
        for encounter_id in ("missing-event", "missing-field", "wrong", "correct")
    ]
    generated = [
        *patients,
        {"resourceType": "Encounter", "id": "missing-field"},
        {"resourceType": "Encounter", "id": "wrong", "subject": {"reference": "Patient/p2"}},
        {"resourceType": "Encounter", "id": "correct", "subject": {"reference": "Patient/p1"}},
    ]
    expected = [*patients, *encounters]
    result = evaluate_fidelity({}, [], generated, expected_resources=expected, reference_resources=generated)
    assert result["reference_accuracy"]["accuracy"] == 1  # Both generated references resolve.
    references = result["expected_reference_metrics"]
    assert references["expected"] == 4
    assert references["missing"] == 2
    assert references["wrong_target"] == 1
    assert references["coverage"]["accuracy"] == 0.5
    assert references["target_accuracy"]["accuracy"] == 0.25
    assert references["present_target_accuracy"]["accuracy"] == 0.5
    scoped = evaluate_reference_resources(generated, expected, field_paths={"Encounter": ["id"]})
    assert scoped["expected_reference_metrics"]["status"] == "not_evaluated"
    assert scoped["expected_reference_metrics"]["coverage"]["accuracy"] is None


def test_external_validator_failure_blocks_even_without_errors_list() -> None:
    result = HybridFhirPipeline(validator=lambda resources: {"valid": False}).run(patient_batch(), patient_profile())
    assert result["metrics"]["final_valid"] is False
    assert any(issue["category"] == "validator" for issue in result["issues"])


def test_no_unapproved_status_or_timezone_is_added() -> None:
    profile = patient_profile()
    revised, changes = revise_profile(profile, [
        {"category": "semantic", "message": "status is required"},
        {"category": "datetime_timezone", "mapping_id": "gender", "message": "timezone missing"},
    ])
    assert revised == profile
    assert changes == []


def test_independent_gold_never_guides_correction() -> None:
    calls: list = []
    def revise(profile: dict, issues: list, records: list) -> dict:
        calls.append(issues)
        assert all(issue["category"] != "independent_fidelity" for issue in issues)
        profile["mappings"][0].update(operation="lookup", table={"man": "male"}, fallback="error")
        return profile

    gold = [{"resourceType": "Patient", "id": "p1", "gender": "female"}]
    result = HybridFhirPipeline(correct=True, profile_reviser=revise).run(patient_batch("man"), patient_profile(), expected_resources=gold)
    assert len(calls) == 1
    assert result["resources"][0]["gender"] == "male"
    assert result["metrics"]["standard_repair_success"] is True
    assert result["metrics"]["repair_success"] is False
    assert result["metrics"]["standard_valid"] is True
    assert result["metrics"]["final_valid"] is False


def test_extra_mapping_is_penalized_and_nested_code_mapping_is_evaluated() -> None:
    reference = patient_profile()
    reference["mappings"] = [
        {"id": "code", "target_path": "code", "operation": "codeable_concept", "code": {"source_field": "clinical_code", "operation": "string"}},
        {"id": "ref", "target_path": "subject", "operation": "reference", "resource_type": "Patient", "source_field": "patient_id"},
    ]
    predicted = deepcopy(reference)
    predicted["mappings"][0]["code"]["source_field"] = "wrong_code"
    predicted["mappings"][1]["resource_type"] = "Encounter"
    predicted["mappings"].append({"id": "extra", "target_path": "valueString", "operation": "string", "source_field": "other"})
    report = mapping_accuracy(predicted, reference)
    assert report["field_mapping_accuracy"]["false_positive"] == 2
    assert report["field_mapping_accuracy"]["false_negative"] == 1
    assert report["field_mapping_accuracy"]["precision"] < 1
    assert report["reference_mapping_accuracy"]["f1"] == 0
    assert report["datatype_mapping_accuracy"]["f1"] < 1


def test_clinical_exporter_ids_do_not_need_to_match() -> None:
    common = {
        "resourceType": "Observation", "status": "final",
        "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e1"},
        "code": {"coding": [{"system": "http://loinc.org", "code": "718-7"}]},
        "effectiveDateTime": "2020-01-01T00:00:00Z", "valueQuantity": {"value": 1.2},
    }
    report = evaluate_reference_resources([{**common, "id": "composite"}], [{**common, "id": "exporter-uuid"}])
    assert report["event_detection"]["f1"] == 1
    assert report["source_field_accuracy"]["accuracy"] == 1
    assert report["issues"] == []


def test_field_scope_is_explicit_and_missing_scoped_field_is_penalized() -> None:
    expected = [{"resourceType": "Patient", "id": "p1", "gender": "male", "name": [{"family": "Smith"}]}]
    generated = [{"resourceType": "Patient", "id": "p1"}]
    report = evaluate_reference_resources(generated, expected, field_paths={"Patient": ["gender"]})
    assert report["source_field_accuracy"]["evaluated"] == 1
    assert report["source_field_accuracy"]["accuracy"] == 0


def test_extra_duplicate_events_are_not_hidden_by_dictionary_lookup() -> None:
    item = {"resourceType": "Patient", "id": "p1", "gender": "male"}
    report = evaluate_reference_resources([item, item], [item])
    assert report["event_detection"]["precision"] == 0.5
    assert report["event_detection"]["recall"] == 1
    assert report["issues"]


def test_composite_reference_identity_mapping_is_evaluated() -> None:
    reference = patient_profile()
    reference["mappings"] = [{
        "id": "reference", "target_path": "encounter", "operation": "reference",
        "resource_type": "Encounter", "identity": {"source_fields": ["patient_id", "visit_id"]},
    }]
    predicted = deepcopy(reference)
    predicted["mappings"][0]["identity"]["source_fields"] = ["patient_id", "wrong_visit"]
    report = mapping_accuracy(predicted, reference)
    assert report["field_mapping_accuracy"]["false_negative"] == 1
    assert report["reference_mapping_accuracy"]["f1"] == 0


def test_repeated_clinical_event_comparison_is_order_independent() -> None:
    common = {
        "resourceType": "Observation", "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"code": "test"}]}, "effectiveDateTime": "2020-01-01",
    }
    first = {**common, "id": "one", "valueString": "negative"}
    second = {**common, "id": "two", "valueString": "positive"}
    report = evaluate_reference_resources([second, first], [first, second])
    assert report["pass"] is True
    assert report["event_detection"]["matched"] == 2


def test_invented_clinical_field_is_a_false_positive_and_blocks_pass() -> None:
    expected = [{"resourceType": "Patient", "id": "p1", "gender": "male"}]
    generated = [{**expected[0], "deceasedBoolean": True}]
    report = evaluate_reference_resources(generated, expected)
    assert report["source_field_accuracy"]["accuracy"] == 1
    assert report["field_detection"]["extra_fields"] == 1
    assert report["field_detection"]["false_positive"] == 1
    assert report["field_detection"]["precision"] < 1
    assert report["pass"] is False
    assert any(issue.get("path") == "deceasedBoolean" for issue in report["issues"])


def test_generated_extra_fields_follow_the_predeclared_common_scope() -> None:
    expected = [{"resourceType": "Patient", "id": "p1", "gender": "male"}]
    generated = [{**expected[0], "deceasedBoolean": True}]
    included = evaluate_reference_resources(generated, expected, field_paths={"Patient": ["gender", "deceasedBoolean"]})
    excluded = evaluate_reference_resources(generated, expected, field_paths={"Patient": ["gender"]})
    assert included["pass"] is False
    assert excluded["pass"] is True
    assert excluded["field_detection"]["false_positive"] == 0


def test_timestamp_spelling_and_decimal_format_are_normalized() -> None:
    expected = [{
        "resourceType": "Observation", "id": "gold", "status": "final",
        "subject": {"reference": "Patient/p1"}, "code": {"coding": [{"code": "test"}]},
        "effectiveDateTime": "2020-01-01T00:00:00Z", "valueQuantity": {"value": 1},
    }]
    generated = [{**expected[0], "id": "generated", "effectiveDateTime": "2020-01-01T00:00:00.000+00:00", "valueQuantity": {"value": 1.0}}]
    report = evaluate_reference_resources(generated, expected)
    assert report["pass"] is True
    assert report["event_detection"]["f1"] == 1
    assert report["field_detection"]["f1"] == 1


def test_date_precision_is_not_silently_reduced_to_match_full_datetime() -> None:
    common = {
        "resourceType": "Condition", "id": "condition", "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"code": "condition-code"}]},
    }
    report = evaluate_reference_resources(
        [{**common, "onsetDateTime": "2020-01-01"}],
        [{**common, "onsetDateTime": "2020-01-01T00:00:00Z"}],
    )
    assert report["pass"] is False
    assert report["event_detection"]["f1"] == 0


def test_date_projection_uses_written_local_date_and_leaves_original_intact() -> None:
    original = [
        {"resourceType": "Condition", "onsetDateTime": "2020-01-01T23:00:00-05:00"},
        {"resourceType": "Condition", "onsetDateTime": "2020-01"},
        {"resourceType": "Observation", "effectiveDateTime": "2020-01-01T23:00:00-05:00"},
    ]
    baseline = deepcopy(original)
    projected = project_comparison_resources(original, date_only_paths={"Condition": ["onsetDateTime"]})
    assert projected[0]["onsetDateTime"] == "2020-01-01"
    assert projected[1:] == original[1:]
    assert original == baseline


def test_opt_in_component_projection_compares_numeric_and_coded_text_events() -> None:
    context = {
        "resourceType": "Observation", "subject": {"reference": "Patient/p1"},
        "encounter": {"reference": "Encounter/e1"}, "effectiveDateTime": "2020-01-01T00:00:00Z",
    }
    systolic = {"code": {"coding": [{"code": "systolic"}]}, "valueQuantity": {"value": 120}}
    concept = {"text": "Normal", "coding": [{"code": "normal"}]}
    panel = {**context, "id": "panel", "code": {"coding": [{"code": "panel"}]}, "component": [
        systolic,
        {"code": {"coding": [{"code": "result"}]}, "valueCodeableConcept": concept},
    ]}
    generated = [
        {**context, **deepcopy(systolic), "id": "measurement"},
        {**context, "id": "result", "code": {"coding": [{"code": "result"}]}, "valueString": "Normal"},
    ]
    options = {
        "component_events": {"Observation": {"path": "component", "inherit": [
            "subject", "encounter", "effectiveDateTime",
        ]}},
        "copy_paths": {"Observation": {"valueString": "valueCodeableConcept.text"}},
    }
    original = deepcopy(panel)
    projected = project_comparison_resources([panel], **options)
    assert len(project_comparison_resources([panel])) == 1
    assert len(projected) == 2
    assert projected[1]["valueString"] == "Normal"
    assert "valueString" not in panel["component"][1]
    report = evaluate_reference_resources(
        project_comparison_resources(generated, **options), projected,
        field_paths={"Observation": ["code", "subject", "encounter", "effectiveDateTime", "valueQuantity", "valueString"]},
    )
    assert report["pass"] is True
    assert report["event_match_coverage"]["accuracy"] == 1
    projected[1]["subject"]["reference"] = "Patient/changed"
    assert projected[0]["subject"]["reference"] == "Patient/p1"
    assert panel == original


def test_value_projection_does_not_replace_existing_value() -> None:
    item = {"resourceType": "Observation", "valueString": "value", "valueCodeableConcept": {"text": "other"}}
    projected = project_comparison_resources(
        [item], copy_paths={"Observation": {"valueString": "valueCodeableConcept.text"}},
    )
    assert projected == [item]


def test_boolean_is_not_equivalent_to_a_numeric_value() -> None:
    expected = [{"resourceType": "Patient", "id": "p1", "deceasedBoolean": True}]
    generated = [{"resourceType": "Patient", "id": "p1", "deceasedBoolean": 1}]
    report = evaluate_reference_resources(generated, expected)
    assert report["pass"] is False
    assert report["field_detection"]["false_positive"] == 1
    assert report["field_detection"]["false_negative"] == 1
