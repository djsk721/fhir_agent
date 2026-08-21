from dataclasses import replace

import pytest

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.llm_pipeline import LlmSqlToFhirPipeline
from fhir_agent_pipeline.llm_query_service import (
    LlmClinicalQueryService,
    LlmQueryTrace,
    QueriedResourceRows,
    strip_database_padding,
)
from fhir_agent_pipeline.resource_renderer import RenderResult
from fhir_agent_pipeline.sql_agent import SqlResultValidationError


class FakePatientQueryService:
    def query(self, resource_type, patient_key, encounter_keys, override):
        return QueriedResourceRows(
            LlmQueryTrace(
                resource_type, "q", "SELECT", "ok", 1, 1
            ),
            [{
                "_patient_key": patient_key,
                "_gender": "F",
                "_death_datetime": None,
            }],
        )

    def encounter_ids(self, rows):
        return []

    def close(self):
        pass


def test_pipeline_accepts_renderer_dependency() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
    )

    class FakeRenderer:
        def generate(
            self, resource_type, rows, patient_key, encounter_keys
        ):
            return RenderResult(
                [{
                    "resourceType": "Patient",
                    "id": str(patient_key),
                    "gender": "female",
                }],
                rows_rendered=1,
            )

    result = LlmSqlToFhirPipeline(
        settings,
        query_service=FakePatientQueryService(),
        renderer=FakeRenderer(),
    ).run(7)

    assert result.validation_report and result.validation_report.valid
    assert result.task_evaluation["source_field_accuracy"]["accuracy"] == 1.0
    assert result.rendering_metrics["Patient"]["llm_calls_used"] == 0


def test_default_pipeline_uses_deterministic_renderer() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
        vectors_enabled=False,
    )

    result = LlmSqlToFhirPipeline(
        settings,
        query_service=FakePatientQueryService(),
    ).run("P-1")

    assert result.generated_resources == [{
        "resourceType": "Patient",
        "id": "P-1",
        "gender": "female",
    }]
    assert result.rendering_metrics["Patient"] == {
        "source_rows": 1,
        "resources_rendered": 1,
        "count_alignment_rate": 1.0,
        "llm_calls_used": 0,
    }
    assert result.trace_dict()["mode"] == "llm-sql-and-deterministic-fhir"


def test_database_padding_is_removed_from_source_rows() -> None:
    assert strip_database_padding([{"x": " A "}]) == [{"x": "A"}]


def test_patient_query_requires_exactly_one_row() -> None:
    service = object.__new__(LlmClinicalQueryService)
    rows = [
        {
            "_patient_key": 7,
            "_gender": "F",
            "_death_datetime": None,
        },
        {
            "_patient_key": 7,
            "_gender": "F",
            "_death_datetime": None,
        },
    ]

    with pytest.raises(SqlResultValidationError, match="정확히 한 행"):
        service._validate_result("Patient", rows, 7, [])


def test_query_rejects_empty_required_values_in_any_row() -> None:
    service = object.__new__(LlmClinicalQueryService)
    rows = [
        {
            "_patient_key": 7,
            "_encounter_key": 11,
            "_encounter_type": "inpatient",
            "_start_datetime": None,
            "_end_datetime": None,
        },
        {
            "_patient_key": 7,
            "_encounter_key": None,
            "_encounter_type": "inpatient",
            "_start_datetime": None,
            "_end_datetime": None,
        },
    ]

    with pytest.raises(
        SqlResultValidationError, match="필수 source 값"
    ) as captured:
        service._validate_result("Encounter", rows, 7, [])

    assert captured.value.category == "result_required_value"
