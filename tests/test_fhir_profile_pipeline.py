from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from research.fhir_profile.agents.mapping import ProfileMappingAgent
from research.fhir_profile.contracts import SourceBatch
from research.fhir_profile.correction.policy import revise_profile
from research.fhir_profile.data.extractors import (
    PredefinedQueryExtractor,
    StaticRecordExtractor,
    build_select,
    load_extract_spec,
)
from research.fhir_profile.pipeline import HybridFhirPipeline, run_comparison
from research.fhir_profile.profiles.schema import load_profile, validate_profile
from research.fhir_profile.renderer.operations import render_records, resource_id


ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_PROFILE = ROOT / "research/fhir_profile/profiles/examples/observation.json"
MIMIC_SPEC = ROOT / "research/fhir_profile/data/specs/mimic_iv.json"


def observation_records() -> list[dict]:
    return [
        {
            "labevent_id": 1,
            "subject_id": 10,
            "hadm_id": 20,
            "itemid": 50912,
            "charttime": "2180-01-02 03:04:05",
            "valuenum": 1.2,
            "value": "1.2",
            "valueuom": "mg/dL",
            "label": "Creatinine",
            "result_status": "final",
        }
    ]


def observation_batch() -> SourceBatch:
    spec = load_extract_spec(MIMIC_SPEC)
    spec["resources"]["Observation"]["columns"].extend(["label", "result_status"])
    return StaticRecordExtractor(spec).extract("Observation", observation_records())


def test_profile_rejects_sql_and_unknown_operation() -> None:
    profile = load_profile(OBSERVATION_PROFILE)
    profile["sql"] = "SELECT 1"
    with pytest.raises(ValueError, match="SQL"):
        validate_profile(profile)
    broken = load_profile(OBSERVATION_PROFILE)
    broken["mappings"][0]["operation"] = "python"
    with pytest.raises(ValueError, match="operation"):
        validate_profile(broken)


def test_renderer_is_deterministic_and_omits_null_values() -> None:
    profile = load_profile(OBSERVATION_PROFILE)
    rows = observation_records()
    first, errors = render_records(profile, rows)
    second, _ = render_records(profile, rows)
    assert errors == []
    assert first == second
    assert first[0]["id"] == "1"
    assert first[0]["effectiveDateTime"] == "2180-01-02T03:04:05Z"
    assert first[0]["subject"] == {"reference": "Patient/10"}
    assert first[0]["valueQuantity"]["value"] == 1.2
    assert first[0]["valueQuantity"]["code"] == "mg/dL"
    assert first[0]["code"]["coding"][0]["code"] == "50912"

    rows[0]["valuenum"] = None
    rendered, errors = render_records(profile, rows)
    assert errors == []
    assert "valueQuantity" not in rendered[0]
    assert rendered[0]["valueString"] == "1.2"


def test_same_renderer_accepts_a_different_dataset_profile() -> None:
    profile = {
        "profile_id": "observation-synthea-v1",
        "profile_version": "1.0.0",
        "target_resource": "Observation",
        "fhir_version": "4.0.1",
        "source": {"dataset": "synthea", "table": "observations"},
        "identity": {"source_fields": ["code", "date"], "separator": "-"},
        "mappings": [
            {
                "id": "status",
                "target_path": "status",
                "operation": "constant",
                "value": "final",
            },
            {
                "id": "code",
                "target_path": "code",
                "operation": "codeable_concept",
                "system": {"operation": "constant", "value": "http://loinc.org"},
                "code": {"source_field": "code", "operation": "string"},
            },
            {
                "id": "subject",
                "target_path": "subject",
                "operation": "reference",
                "source_field": "patient",
                "resource_type": "Patient",
            },
        ],
    }
    resources, errors = render_records(
        validate_profile(profile),
        [{"patient": "abc", "code": "718-7", "date": "2020-01-01"}],
    )
    assert errors == []
    assert resources[0]["id"] == resource_id(profile, {"code": "718-7", "date": "2020-01-01"})
    assert len(resources[0]["id"]) == 64  # ambiguous composite parts are hashed
    assert resources[0]["subject"] == {"reference": "Patient/abc"}


def test_predefined_extractor_uses_bound_parameters() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE labevents ("
                "labevent_id INTEGER, subject_id INTEGER, hadm_id INTEGER, "
                "itemid INTEGER, charttime TEXT, valuenum REAL, value TEXT, valueuom TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO labevents VALUES "
                "(1, 10, 20, 50912, '2180-01-02 03:04:05', 1.2, NULL, 'mg/dL'),"
                "(2, 10, 21, 50912, '2180-01-03 03:04:05', 1.4, NULL, 'mg/dL'),"
                "(3, 11, 20, 50912, '2180-01-03 03:04:05', 1.4, NULL, 'mg/dL')"
            )
        )
    spec = load_extract_spec(MIMIC_SPEC)
    spec["resources"]["Observation"].pop("schema")
    extractor = PredefinedQueryExtractor(engine, spec)
    batch = extractor.extract("Observation", 10, [20])
    assert [row["labevent_id"] for row in batch.records] == [1]
    assert "SELECT" in build_select(
        spec["resources"]["Observation"], with_encounters=True
    )
    with pytest.raises(ValueError):
        build_select(
            {**spec["resources"]["Observation"], "table": "labevents;drop"},
            with_encounters=False,
        )


def test_self_correction_adds_missing_status_without_sql_changes() -> None:
    profile = load_profile(OBSERVATION_PROFILE)
    profile["mappings"] = [
        mapping for mapping in profile["mappings"] if mapping["id"] != "status"
    ]
    def revise(current: dict, issues: list, records: list) -> dict:
        assert any("status" in item["message"] for item in issues)
        assert records[0]["result_status"] == "final"
        current["mappings"].append({"id": "status", "target_path": "status", "operation": "string", "source_field": "result_status"})
        return current

    result = HybridFhirPipeline(correct=True, max_iterations=2, profile_reviser=revise).run(
        observation_batch(), profile
    )
    assert result["resources"][0]["status"] == "final"
    assert result["metrics"]["repair_attempts"] == 1
    assert result["metrics"]["repair_success"] is True
    assert result["metrics"]["fidelity_preserved"] is None
    assert result["profile_execution_consistency"]["source_field_accuracy"]["accuracy"] == 1.0
    assert "sql" not in json.dumps(result["profile"])


def test_ucum_correction_changes_profile_not_source_rows() -> None:
    profile = load_profile(OBSERVATION_PROFILE)
    for mapping in profile["mappings"]:
        if mapping["id"] == "ucum-code":
            mapping["operation"] = "copy"
            mapping.pop("table", None)
    records = observation_records()
    records[0]["valueuom"] = "mm Hg"
    revised, changes = revise_profile(
        validate_profile(profile),
        [
            {
                "category": "validator",
                "mapping_id": "ucum-code",
                "message": "Unknown code 'mm Hg' in UCUM",
            }
        ],
        records,
        unit_aliases={"mm Hg": "mm[Hg]"},
    )
    assert changes[0]["change"] == "apply explicitly configured UCUM lookup"
    resources, errors = render_records(revised, records)
    assert errors == []
    assert resources[0]["valueQuantity"]["code"] == "mm[Hg]"
    assert records[0]["valueuom"] == "mm Hg"


def test_mapping_agent_accepts_profile_json_only() -> None:
    profile = load_profile(OBSERVATION_PROFILE)

    class FakeClient:
        def chat(self, system_prompt: str, user_prompt: str) -> str:
            assert "SQL" in system_prompt
            return json.dumps(profile)

    generated = ProfileMappingAgent(FakeClient()).generate(
        {"columns": list(observation_records()[0])},
        "Observation.effectiveDateTime is dateTime",
        "Observation",
    )
    assert generated["target_resource"] == "Observation"


def test_comparison_uses_the_same_source_records() -> None:
    profile = load_profile(OBSERVATION_PROFILE)
    batch = observation_batch()

    def direct(source: SourceBatch) -> list[dict]:
        assert source.records == batch.records
        resources, _ = render_records(profile, list(source.records))
        return resources

    report = run_comparison(
        batch,
        profile,
        direct_generator=direct,
        reference_profile=profile,
    )
    assert report["record_count"] == 1
    assert report["groups"]["P1"]["source_field_accuracy"] is None
    assert report["groups"]["P1"]["profile_execution_consistency"] == 1.0
    assert report["groups"]["P1"]["llm_calls"] is None
    assert report["groups"]["B1"]["llm_calls"] is None
    assert report["rq1_mapping_accuracy"]["resource_mapping_accuracy"] == 1.0
    assert report["rq1_mapping_accuracy"]["field_mapping_accuracy"]["accuracy"] == 1.0
