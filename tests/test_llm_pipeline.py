from dataclasses import replace

import pytest

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.llm_pipeline import LlmSqlToFhirPipeline
from fhir_agent_pipeline.llm_query_service import (
    EncounterQueryContext,
    LlmClinicalQueryService,
    LlmQueryTrace,
    QueriedResourceRows,
    normalize_contract_aliases,
    qualify_cross_encounter_resource_keys,
    strip_database_padding,
)
from fhir_agent_pipeline.sql_agent import SqlResultValidationError
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


def test_resource_row_limit_never_exceeds_sql_safety_limit() -> None:
    service = object.__new__(LlmClinicalQueryService)
    service.settings = replace(
        load_settings(require_database=False),
        sql_max_rows=200,
        max_encounters=100,
        max_observations_per_encounter=100,
    )

    assert service._row_limit("Patient", 0) == 1
    assert service._row_limit("Encounter", 0) == 100
    assert service._row_limit("Observation", 1) == 100
    assert service._row_limit("Observation", 5) == 200


def test_pipeline_rechecks_all_empty_clinical_relationships() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=[
            "Patient",
            "Encounter",
            "Condition",
            "Procedure",
        ],
        fhir_validator_jar=None,
    )

    class FakeQueryService:
        def __init__(self):
            self.calls = []

        def query(
            self,
            resource_type,
            patient_key,
            encounter_context,
            override,
            **kwargs,
        ):
            semantic_retry = kwargs.get("semantic_relationship_retry", False)
            self.calls.append((resource_type, encounter_context, semantic_retry))
            rows = {
                "Patient": [{
                    "_patient_key": patient_key,
                    "_gender": None,
                    "_death_datetime": None,
                }],
                "Encounter": [{
                    "_patient_key": patient_key,
                    "_encounter_key": "V-1",
                    "_encounter_type": None,
                    "_start_datetime": None,
                    "_end_datetime": None,
                }],
                "Condition": (
                    [{
                        "_patient_key": patient_key,
                        "_encounter_key": "V-1",
                        "_resource_key": "C-1",
                        "_code": "DX",
                        "_code_system": None,
                        "_display": None,
                    }]
                    if semantic_retry
                    else []
                ),
                "Procedure": [],
            }[resource_type]
            return QueriedResourceRows(
                LlmQueryTrace(
                    resource_type,
                    "q",
                    f"SELECT FROM source.{resource_type.lower()}",
                    "ok",
                    len(rows),
                    1,
                    encounter_context_relations=(
                        encounter_context.source_relations
                    ),
                    semantic_relationship_retry=semantic_retry,
                ),
                rows,
            )

        def encounter_ids(self, rows):
            return [row["_encounter_key"] for row in rows]

        def build_encounter_context(self, queried):
            return EncounterQueryContext(
                keys=("V-1",),
                source_relations=("source.visit_event",),
                sql=queried.trace.sql,
                schema_hint="source.visit_event: person_key, visit_key",
            )

        def close(self):
            pass

    class FakeResourceService:
        def generate(self, resource_type, rows, patient_key):
            resources = []
            if resource_type == "Patient":
                resources = [{"resourceType": "Patient", "id": str(patient_key)}]
            elif resource_type == "Encounter":
                resources = [{
                    "resourceType": "Encounter",
                    "id": "V-1",
                    "status": "in-progress",
                    "class": {
                        "system": (
                            "http://terminology.hl7.org/CodeSystem/v3-ActCode"
                        ),
                        "code": "IMP",
                    },
                    "subject": {"reference": f"Patient/{patient_key}"},
                }]
            elif resource_type == "Condition" and rows:
                resources = [{
                    "resourceType": "Condition",
                    "id": "C-1",
                    "code": {"coding": [{"code": "DX"}]},
                    "subject": {"reference": f"Patient/{patient_key}"},
                    "encounter": {"reference": "Encounter/V-1"},
                }]
            batches = 1 if rows else 0
            return FhirGenerationResult(
                resources,
                batches,
                batches,
                batches,
                0,
                0,
                0,
            )

    query_service = FakeQueryService()
    result = LlmSqlToFhirPipeline(
        settings,
        query_service=query_service,
        resource_service=FakeResourceService(),
    ).run("P-1")

    semantic_calls = [
        item for item in query_service.calls if item[2]
    ]
    assert [item[0] for item in semantic_calls] == [
        "Condition",
        "Procedure",
    ]
    assert all(
        item[1].source_relations == ("source.visit_event",)
        for item in semantic_calls
    )
    assert result.source_row_counts["Condition"] == 1
    assert result.source_row_counts["Procedure"] == 0
    assert (
        result.task_evaluation["sql"]["semantic_relationship_rechecks"]
        == 2
    )
    assert (
        result.task_evaluation["sql"]["semantic_relationship_recoveries"]
        == 1
    )


def test_encounter_result_requires_unique_canonical_keys() -> None:
    service = object.__new__(LlmClinicalQueryService)
    duplicate_rows = [
        {
            "_patient_key": "P-1",
            "_encounter_key": "V-1",
            "_encounter_type": None,
            "_start_datetime": None,
            "_end_datetime": None,
        },
        {
            "_patient_key": "P-1",
            "_encounter_key": "V-1",
            "_encounter_type": None,
            "_start_datetime": None,
            "_end_datetime": None,
        },
    ]

    with pytest.raises(SqlResultValidationError) as captured:
        service._validate_result("Encounter", duplicate_rows, "P-1", [])

    assert captured.value.category == "result_encounter_identity"


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


def test_direct_source_property_uses_fhir_schema_shape() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        vectors_enabled=False,
    )

    class FakeAgent:
        def run(self, *args, **kwargs):
            return {
                "resources": [
                    {
                        "source_row": 0,
                        "resource": {
                            "resourceType": "Observation",
                            "interpretation": "abnormal",
                        },
                    },
                    {
                        "source_row": 1,
                        "resource": {
                            "resourceType": "Observation",
                            "interpretation": "invented",
                        },
                    },
                ]
            }

    service = FhirLlmResourceService(
        settings,
        get_fhir_schema(settings.fhir_schema_path),
        FakeAgent(),
        None,
    )
    base_row = {
        "_patient_key": "P-1",
        "_encounter_key": "V-1",
        "_code": "CODE-1",
        "_event_datetime": "2026-07-26 12:00:00",
        "_value_number": 2.5,
    }
    result = service.generate(
        "Observation",
        [
            {
                **base_row,
                "_resource_key": "O-1",
                "_interpretation": "abnormal",
            },
            {
                **base_row,
                "_resource_key": "O-2",
                "_interpretation": None,
            },
        ],
        "P-1",
    )

    assert result.resources[0]["interpretation"] == [{"text": "abnormal"}]
    assert "interpretation" not in result.resources[1]


def test_padding_and_empty_array_validation() -> None:
    assert strip_database_padding([{"x": " A "}]) == [{"x": "A"}]
    assert empty_array_errors({"name": []})


def test_contract_alias_normalization_only_restores_known_aliases() -> None:
    rows = [{
        "patient_key": "P-1",
        "encounter_key": "V-1",
        "resource_key": "C-1",
        "code": "DX",
        "code_system": None,
        "display": None,
        "unrelated": "kept",
    }]

    changes = normalize_contract_aliases("Condition", rows)

    assert rows == [{
        "_patient_key": "P-1",
        "_encounter_key": "V-1",
        "_resource_key": "C-1",
        "_code": "DX",
        "_code_system": None,
        "_display": None,
        "unrelated": "kept",
    }]
    assert "patient_key->_patient_key" in changes
    assert all("unrelated" not in item for item in changes)


def test_duplicate_resource_keys_are_only_qualified_across_encounters() -> None:
    rows = [
        {"_encounter_key": "V-1", "_resource_key": "CODE"},
        {"_encounter_key": "V-2", "_resource_key": "CODE"},
    ]

    changed = qualify_cross_encounter_resource_keys("Condition", rows)

    assert changed == 2
    assert [row["_resource_key"] for row in rows] == [
        "V-1|CODE",
        "V-2|CODE",
    ]

    same_encounter = [
        {"_encounter_key": "V-1", "_resource_key": "CODE"},
        {"_encounter_key": "V-1", "_resource_key": "CODE"},
    ]
    qualify_cross_encounter_resource_keys("Condition", same_encounter)
    assert len({row["_resource_key"] for row in same_encounter}) == 1


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
