from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.evaluation import _equivalent, evaluate_task_output
from fhir_agent_pipeline.mapping_config import MimicMappingConfig


def _mapping() -> MimicMappingConfig:
    settings = load_settings(require_database=False)
    return MimicMappingConfig.load(settings.mimic_mapping_config_path)


def test_task_evaluation_compares_source_values_and_resource_f1() -> None:
    mapping = _mapping()
    source = {
        "Patient": [{"subject_id": 7, "gender": "F", "dod": None}],
        "Condition": [
            {
                "subject_id": 7,
                "hadm_id": 9,
                "seq_num": 1,
                "icd_code": "A01.0",
                "icd_version": 10,
                "long_title": "Typhoid fever",
                "fhir_icd_system": "http://hl7.org/fhir/sid/icd-10-cm",
            }
        ],
    }
    generated = {
        "Patient": [
            {
                "resourceType": "Patient",
                "id": "7",
                "identifier": [
                    {"system": "urn:mimic:subject-id", "value": "7"}
                ],
                "gender": "female",
            }
        ],
        "Condition": [
            {
                "resourceType": "Condition",
                "id": "9-1",
                "identifier": [
                    {"system": "urn:mimic:diagnosis-icd", "value": "9-1"}
                ],
                "code": {
                    "coding": [
                        {
                            "system": "http://hl7.org/fhir/sid/icd-10-cm",
                            "code": "A01.0",
                            "display": "Typhoid fever",
                        }
                    ]
                },
                "category": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                "code": "encounter-diagnosis",
                            }
                        ]
                    }
                ],
                "subject": {"reference": "Patient/7"},
                "encounter": {"reference": "Encounter/9"},
            }
        ],
    }

    result = evaluate_task_output(mapping, source, generated, "Z")

    assert result["source_field_accuracy"]["accuracy"] == 1.0
    assert result["resource_identity_metrics"]["Condition"]["f1"] == 1.0
    assert (
        result["resource_count_alignment"]["overall"]["alignment_rate"] == 1.0
    )
    assert result["reference_accuracy"]["all"]["accuracy"] == 1.0
    assert result["terminology_accuracy"]["coding_system_and_code"]["accuracy"] == 1.0


def test_task_evaluation_exposes_wrong_value_missing_resource_and_extra_field() -> None:
    mapping = _mapping()
    source = {
        "Patient": [{"subject_id": 7, "gender": "F", "dod": None}],
        "Observation": [
            {
                "subject_id": 7,
                "hadm_id": 9,
                "labevent_id": 11,
                "row_id": None,
                "itemid": 50882,
                "charttime": "2200-01-01T12:00:00",
                "valuenum": 1.0,
                "value": "1.0",
                "valueuom": "mEq/L",
                "ref_range_lower": None,
                "ref_range_upper": None,
                "flag": None,
            }
        ],
    }
    generated = {
        "Patient": [
            {
                "resourceType": "Patient",
                "id": "7",
                "identifier": [
                    {"system": "urn:mimic:subject-id", "value": "wrong"}
                ],
                "gender": "female",
                "birthDate": "2000-01-01",
            }
        ],
        "Observation": [],
    }

    result = evaluate_task_output(mapping, source, generated, "Z")

    assert result["source_field_accuracy"]["accuracy"] < 1.0
    assert result["resource_identity_metrics"]["Observation"]["f1"] == 0.0
    assert result["unsupported_top_level_fields"]["count"] == 1


def test_observation_exposes_detection_value_unit_and_time_metrics() -> None:
    mapping = _mapping()
    source = {
        "Observation": [
            {
                "subject_id": 7,
                "hadm_id": 9,
                "labevent_id": 11,
                "row_id": None,
                "itemid": 50882,
                "charttime": "2200-01-01T12:00:00",
                "valuenum": 1.0,
                "value": "1.0",
                "valueuom": "mEq/L",
                "ref_range_lower": None,
                "ref_range_upper": None,
                "flag": None,
            }
        ]
    }
    generated = {
        "Observation": [
            {
                "resourceType": "Observation",
                "id": "11",
                "identifier": [
                    {"system": "urn:mimic:labevent-id", "value": "11"}
                ],
                "status": "final",
                "category": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                "code": "laboratory",
                            }
                        ]
                    }
                ],
                "code": {
                    "coding": [
                        {"system": "urn:mimic:itemid", "code": "50882"}
                    ]
                },
                "subject": {"reference": "Patient/7"},
                "encounter": {"reference": "Encounter/9"},
                "effectiveDateTime": "2200-01-01T12:00:00Z",
                "valueQuantity": {
                    "value": 1.0,
                    "unit": "mEq/L",
                    "system": "http://unitsofmeasure.org",
                    "code": "meq/L",
                },
            }
        ]
    }

    result = evaluate_task_output(mapping, source, generated, "Z")

    observation = result["observation_quality"]
    assert observation["detection"]["f1"] == 1.0
    assert observation["value_accuracy"] == {
        "correct": 1,
        "evaluated": 1,
        "accuracy": 1.0,
    }
    assert observation["unit_accuracy"]["accuracy"] == 1.0
    assert observation["unit_accuracy"]["evaluated"] == 3
    assert observation["time_accuracy"]["accuracy"] == 1.0


def test_date_only_source_equals_same_fhir_midnight() -> None:
    assert _equivalent("2180-08-06T00:00:00Z", "2180-08-06")
    assert not _equivalent("2180-08-07T00:00:00Z", "2180-08-06")
