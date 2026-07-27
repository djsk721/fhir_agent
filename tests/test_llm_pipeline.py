from dataclasses import replace

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.llm_pipeline import LlmSqlToFhirPipeline
from fhir_agent_pipeline.llm_query_service import (
    LlmQueryTrace,
    QueriedResourceRows,
    strip_database_padding,
)
from fhir_agent_pipeline.llm_resource_service import (
    FhirGenerationResult,
    FhirLlmResourceService,
    _fhir_datetime,
    empty_array_errors,
)


def test_llm_pipeline_uses_generic_sql_aliases() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
    )

    class FakeQueryService:
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

    class FakeResourceService:
        def generate(self, resource_type, rows, patient_key):
            return FhirGenerationResult(
                [{"resourceType": "Patient", "id": str(patient_key), "gender": "female"}],
                1, 1, 1, 0, 0, 0,
            )

    result = LlmSqlToFhirPipeline(
        settings,
        query_service=FakeQueryService(),
        resource_service=FakeResourceService(),
    ).run(7)

    assert result.validation_report and result.validation_report.valid
    assert result.task_evaluation["source_field_accuracy"]["accuracy"] == 1.0


def test_resource_service_enforces_stable_id_and_removes_empty_arrays() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
        vectors_enabled=False,
    )

    class FakeAgent:
        def run(self, *args, **kwargs):
            return {
                "resources": [{
                    "source_row": 0,
                    "resource": {
                        "resourceType": "Patient",
                        "id": "wrong",
                        "gender": "female",
                        "name": [],
                        "meta": {
                            "profile": ["http://example.org/InventedProfile"],
                        },
                    },
                }]
            }

    service = FhirLlmResourceService(
        settings,
        get_fhir_schema(settings.fhir_schema_path),
        FakeAgent(),
        None,
    )
    result = service.generate(
        "Patient",
        [{"_patient_key": "P-1", "_gender": "F", "_death_datetime": None}],
        "P-1",
    )

    assert result.resources[0]["id"] == "P-1"
    assert "name" not in result.resources[0]
    assert "meta" not in result.resources[0]


def test_resource_service_preserves_source_code_without_system() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        vectors_enabled=False,
    )

    class FakeAgent:
        def run(self, *args, **kwargs):
            return {
                "resources": [{
                    "source_row": 0,
                    "resource": {"resourceType": "Condition"},
                }]
            }

    service = FhirLlmResourceService(
        settings,
        get_fhir_schema(settings.fhir_schema_path),
        FakeAgent(),
        None,
    )
    result = service.generate(
        "Condition",
        [{
            "_patient_key": "P-1",
            "_encounter_key": "V-1",
            "_resource_key": "C-1",
            "_code": "CODE-1",
            "_code_system": None,
            "_display": None,
        }],
        "P-1",
    )

    assert result.resources[0]["code"]["coding"] == [{"code": "CODE-1"}]


def test_observation_reference_range_is_normalized_from_source() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        vectors_enabled=False,
    )

    class FakeAgent:
        def run(self, *args, **kwargs):
            return {
                "resources": [{
                    "source_row": 0,
                    "resource": {
                        "resourceType": "Observation",
                        "referenceRange": {"low": {"value": 1}},
                        "valueCodeableConcept": {"text": "invented"},
                    },
                }]
            }

    service = FhirLlmResourceService(
        settings,
        get_fhir_schema(settings.fhir_schema_path),
        FakeAgent(),
        None,
    )
    result = service.generate(
        "Observation",
        [{
            "_patient_key": "P-1",
            "_encounter_key": "V-1",
            "_resource_key": "O-1",
            "_code": "CODE-1",
            "_code_system": None,
            "_display": None,
            "_event_datetime": "2026-07-26 12:00:00",
            "_value_number": "2.5",
            "_value_text": None,
            "_unit": None,
            "_unit_system": None,
            "_unit_code": None,
            "_reference_low": "1.0",
            "_reference_high": "3.0",
            "_interpretation": None,
        }],
        "P-1",
    )

    assert result.resources[0]["referenceRange"] == [{
        "low": {"value": 1},
        "high": {"value": 3},
    }]
    assert "valueCodeableConcept" not in result.resources[0]
    assert result.resources[0]["valueQuantity"]["value"] == 2.5


def test_padding_and_empty_array_validation() -> None:
    assert strip_database_padding([{"x": " A "}]) == [{"x": "A"}]
    assert empty_array_errors({"name": []})


def test_fhir_datetime_keeps_date_only_and_zones_timestamp() -> None:
    assert _fhir_datetime("2026-07-26", "Z") == "2026-07-26"
    assert _fhir_datetime("2026-07-26 12:34:56", "Z") == "2026-07-26T12:34:56Z"


def test_normalizer_keeps_only_source_backed_choice_fields() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        vectors_enabled=False,
    )
    service = FhirLlmResourceService(
        settings,
        get_fhir_schema(settings.fhir_schema_path),
        object(),
        None,
    )
    patient = service._normalize(
        "Patient",
        {
            "_patient_key": "P-1",
            "_gender": "F",
            "_death_datetime": "2026-07-26 12:34:56",
        },
        {
            "resourceType": "Patient",
            "deceasedBoolean": True,
            "deceasedDateTime": "2000-01-01T00:00:00Z",
        },
        "P-1",
    )
    observation = service._normalize(
        "Observation",
        {
            "_resource_key": "O-1",
            "_patient_key": "P-1",
            "_encounter_key": "E-1",
            "_code": "C-1",
            "_event_datetime": "2026-07-26 12:34:56",
            "_value_number": 1,
        },
        {
            "resourceType": "Observation",
            "effectiveInstant": "2000-01-01T00:00:00Z",
            "effectiveDateTime": "2000-01-01T00:00:00Z",
            "valueString": "invented",
        },
        "P-1",
    )

    assert patient["deceasedDateTime"] == "2026-07-26T12:34:56Z"
    assert "deceasedBoolean" not in patient
    assert observation["effectiveDateTime"] == "2026-07-26T12:34:56Z"
    assert "effectiveInstant" not in observation
    assert observation["valueQuantity"]["value"] == 1
    assert "valueString" not in observation
