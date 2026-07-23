from dataclasses import replace

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.validation import (
    _validator_issue_counts,
    validate_bundle_detailed,
)
from fhir_agent_pipeline.fhir_models import build_bundle
import pytest


def valid_bundle() -> dict:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"fullUrl": "urn:resource:patient:p1", "resource": {"resourceType": "Patient", "id": "p1", "name": [{"family": "Kim"}]}},
            {"fullUrl": "urn:resource:encounter:e1", "resource": {"resourceType": "Encounter", "id": "e1", "status": "finished", "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"}, "subject": {"reference": "Patient/p1"}}},
            {"fullUrl": "urn:resource:condition:c1", "resource": {"resourceType": "Condition", "id": "c1", "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "B18.2"}]}, "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e1"}}},
            {"fullUrl": "urn:resource:procedure:pr1", "resource": {"resourceType": "Procedure", "id": "pr1", "status": "completed", "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "0W9G3ZZ"}]}, "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e1"}}},
            {"fullUrl": "urn:resource:observation:o1", "resource": {"resourceType": "Observation", "id": "o1", "status": "final", "code": {"coding": [{"system": "http://loinc.org", "code": "718-7"}]}, "subject": {"reference": "Patient/p1"}, "encounter": {"reference": "Encounter/e1"}, "valueQuantity": {"value": 12.3}}},
        ],
    }


def test_full_fhir_schema_and_quality_metrics() -> None:
    settings = replace(load_settings(require_database=False), fhir_validator_jar=None)
    report = validate_bundle_detailed(valid_bundle(), settings)
    assert report.valid
    assert report.metrics["schema_valid_resource_ratio"] == 1.0
    assert report.metrics["reference_resolution_ratio"] == 1.0


def test_official_schema_rejects_missing_required_encounter_status() -> None:
    bundle = valid_bundle()
    del bundle["entry"][1]["resource"]["status"]
    settings = replace(load_settings(require_database=False), fhir_validator_jar=None)
    report = validate_bundle_detailed(bundle, settings)
    assert not report.valid
    assert report.metrics["schema_valid_resource_ratio"] < 1.0


def test_unresolved_uuid_reference_is_reported() -> None:
    bundle = valid_bundle()
    bundle["entry"][4]["resource"]["subject"]["reference"] = (
        "urn:uuid:00000000-0000-0000-0000-000000000000"
    )
    settings = replace(load_settings(require_database=False), fhir_validator_jar=None)
    report = validate_bundle_detailed(bundle, settings)
    assert not report.valid
    assert report.metrics["reference_resolution_ratio"] < 1.0


def test_bundle_supports_repeated_resource_types_and_rejects_duplicate_ids() -> None:
    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
        {"resourceType": "Condition", "id": "c2"},
    ]
    assert len(build_bundle(resources)["entry"]) == 3
    with pytest.raises(ValueError, match="중복"):
        build_bundle([resources[0], resources[0]])


def test_validator_severity_output_is_structured() -> None:
    counts = _validator_issue_counts(
        "[1,2] Patient: Warning - narrative missing\n"
        "[2,3] Condition: Error - invalid code\n"
        "[3,4] Bundle: Information - checked"
    )
    assert counts == {"fatal": 0, "error": 1, "warning": 1, "information": 1}
