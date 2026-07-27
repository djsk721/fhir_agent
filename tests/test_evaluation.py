from fhir_agent_pipeline.evaluation import evaluate_task_output
from fhir_agent_pipeline.source_contract import (
    encounter_reference,
    patient_reference,
    stable_resource_id,
)


def test_generic_contract_evaluation_matches_source_without_mapping() -> None:
    source = {
        "Patient": [
            {
                "_patient_key": "P-1",
                "_gender": "F",
                "_death_datetime": None,
            }
        ]
    }
    generated = {
        "Patient": [
            {"resourceType": "Patient", "id": "P-1", "gender": "female"}
        ]
    }

    result = evaluate_task_output(source, generated, "Z")

    assert result["task_quality_pass"]
    assert result["source_field_accuracy"]["accuracy"] == 1.0
    assert result["resource_count_alignment"]["overall"]["expected"] == 1


def test_date_only_procedure_time_is_not_given_a_timezone_suffix() -> None:
    row = {
        "_patient_key": "P-1",
        "_encounter_key": "V-1",
        "_resource_key": "PR-1",
        "_code": "CODE-1",
        "_code_system": None,
        "_display": None,
        "_event_datetime": "2026-07-26",
    }
    generated = {
        "Procedure": [{
            "resourceType": "Procedure",
            "id": stable_resource_id("Procedure", row),
            "subject": {"reference": patient_reference("P-1")},
            "encounter": {"reference": encounter_reference("V-1")},
            "code": {"coding": [{"code": "CODE-1"}]},
            "performedDateTime": "2026-07-26",
        }]
    }

    result = evaluate_task_output({"Procedure": [row]}, generated, "Z")

    assert result["task_quality_pass"]


def test_observation_numeric_strings_compare_by_numeric_value() -> None:
    row = {
        "_patient_key": "P-1",
        "_encounter_key": "E-1",
        "_resource_key": "O-1",
        "_code": "CODE-1",
        "_code_system": None,
        "_display": None,
        "_event_datetime": "2026-07-26 12:00:00",
        "_value_number": "80.0",
        "_value_text": None,
        "_unit": "kg",
        "_unit_system": None,
        "_unit_code": None,
    }
    generated = {
        "Observation": [{
            "resourceType": "Observation",
            "id": stable_resource_id("Observation", row),
            "subject": {"reference": patient_reference("P-1")},
            "encounter": {"reference": encounter_reference("E-1")},
            "code": {"coding": [{"code": "CODE-1"}]},
            "effectiveDateTime": "2026-07-26T12:00:00Z",
            "valueQuantity": {"value": 80, "unit": "kg"},
        }]
    }

    result = evaluate_task_output({"Observation": [row]}, generated, "Z")

    assert result["observation_quality"]["value_accuracy"]["accuracy"] == 1.0
    assert result["task_quality_pass"]
