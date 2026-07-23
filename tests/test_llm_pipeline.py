from dataclasses import replace

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.llm_pipeline import LlmSqlToFhirPipeline
from fhir_agent_pipeline.llm_query_service import (
    LlmClinicalQueryService,
    LlmQueryTrace,
    QueriedResourceRows,
    strip_database_padding,
)
from fhir_agent_pipeline.llm_resource_service import (
    FhirGenerationResult,
    FhirLlmResourceService,
    empty_array_errors,
)
from fhir_agent_pipeline.mapping_config import MimicMappingConfig


def patient_resource(patient_id: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [{"system": "urn:mimic:subject-id", "value": patient_id}],
        "gender": "female",
    }


def test_llm_pipeline_orchestrates_query_generation_and_validation() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
    )

    class FakeQueryService:
        closed = False

        def query(self, resource_type, subject_id, admission_ids, admission_id):
            assert (resource_type, subject_id, admission_ids, admission_id) == (
                "Patient", 7, [], None
            )
            return QueriedResourceRows(
                trace=LlmQueryTrace(
                    "Patient", "patient query", "SELECT ...", "환자 조회", 1, 1
                ),
                rows=[{"subject_id": 7, "gender": "F", "dod": None}],
            )

        def admission_ids(self, rows):
            return []

        def close(self):
            self.closed = True

    class FakeResourceService:
        def generate(self, resource_type, rows, subject_id):
            assert resource_type == "Patient"
            assert rows[0]["subject_id"] == subject_id == 7
            return FhirGenerationResult(
                resources=[patient_resource("7")],
                llm_calls_used=2,
                batches=1,
                first_pass_valid_batches=0,
                repaired_batches=1,
                repair_attempts=1,
                initial_error_count=1,
            )

    query_service = FakeQueryService()
    result = LlmSqlToFhirPipeline(
        settings,
        query_service=query_service,
        resource_service=FakeResourceService(),
    ).run(7)

    assert result.validation_report and result.validation_report.valid
    assert result.generated_resources[0]["id"] == "7"
    assert result.source_row_counts == {"Patient": 1}
    assert result.generation_llm_calls == {"Patient": 2}
    assert result.llm_calls_used == 3
    assert result.task_evaluation["source_field_accuracy"]["accuracy"] == 1.0
    assert (
        result.generation_metrics["Patient"]["first_pass_valid_rate"] == 0.0
    )
    assert result.query_traces[0].row_count == 1
    assert query_service.closed


def test_fhir_resource_service_retries_invalid_llm_identity() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
        llm_fhir_max_attempts=2,
    )
    mapping = MimicMappingConfig.load(settings.mimic_mapping_config_path)
    schema = get_fhir_schema(settings.fhir_schema_path)

    class FakeFhirAgent:
        calls = 0

        def run(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "resources": [
                    {
                        "source_row": 0,
                        "resource": patient_resource(
                            "wrong" if self.calls == 1 else "7"
                        ),
                    }
                ]
            }

    class FakeVectorStore:
        def search(self, *_args, **_kwargs):
            return [{"content": "FHIR R4 Patient identifier and gender rules"}]

    service = FhirLlmResourceService(
        settings, mapping, schema, FakeFhirAgent(), FakeVectorStore()
    )
    result = service.generate(
        "Patient", [{"subject_id": 7, "gender": "F", "dod": None}], 7
    )

    assert result.llm_calls_used == 2
    assert result.resources[0]["id"] == "7"
    assert result.metrics()["repair_success_rate"] == 1.0
    assert result.initial_error_count > 0


def test_fhir_resource_service_supports_no_rag_ablation_baseline() -> None:
    settings = replace(
        load_settings(require_database=False),
        vectors_enabled=False,
        fhir_output_resources=["Patient"],
        fhir_validator_jar=None,
    )
    mapping = MimicMappingConfig.load(settings.mimic_mapping_config_path)
    schema = get_fhir_schema(settings.fhir_schema_path)

    class FakeFhirAgent:
        def run(self, *_args, **_kwargs):
            return {
                "resources": [
                    {"source_row": 0, "resource": patient_resource("7")}
                ]
            }

    service = FhirLlmResourceService(
        settings, mapping, schema, FakeFhirAgent(), None
    )
    result = service.generate(
        "Patient", [{"subject_id": 7, "gender": "F", "dod": None}], 7
    )

    assert result.resources[0]["id"] == "7"
    assert result.metrics()["first_pass_valid_rate"] == 1.0


def test_fhir_resource_service_retries_unsupported_patient_birth_date() -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_validator_jar=None,
        llm_fhir_max_attempts=2,
    )
    mapping = MimicMappingConfig.load(settings.mimic_mapping_config_path)
    schema = get_fhir_schema(settings.fhir_schema_path)

    class FakeFhirAgent:
        calls = 0

        def run(self, *_args, **_kwargs):
            self.calls += 1
            resource = patient_resource("7")
            if self.calls == 1:
                resource["birthDate"] = "2180-09-09"
            return {"resources": [{"source_row": 0, "resource": resource}]}

    class FakeVectorStore:
        def search(self, *_args, **_kwargs):
            return [{"content": "FHIR R4 Patient identifier and gender rules"}]

    service = FhirLlmResourceService(
        settings, mapping, schema, FakeFhirAgent(), FakeVectorStore()
    )
    result = service.generate(
        "Patient", [{"subject_id": 7, "gender": "F", "dod": None}], 7
    )

    assert result.llm_calls_used == 2
    assert "birthDate" not in result.resources[0]


def test_database_char_padding_is_removed_without_changing_other_values() -> None:
    rows = strip_database_padding(
        [{"icd_code": "07054  ", "value": "  positive ", "number": 3}]
    )
    assert rows == [{"icd_code": "07054", "value": "positive", "number": 3}]


def test_llm_source_preparation_applies_configured_icd_format() -> None:
    settings = load_settings(require_database=False)
    service = object.__new__(LlmClinicalQueryService)
    service.mapping = MimicMappingConfig.load(settings.mimic_mapping_config_path)
    rows = service._prepare_rows(
        "Condition", [{"icd_code": "07054  ", "icd_version": 9}]
    )
    assert rows[0]["icd_code"] == "070.54"
    assert rows[0]["fhir_icd_system"] == "http://hl7.org/fhir/sid/icd-9-cm"


def test_empty_fhir_arrays_are_rejected_before_external_validator() -> None:
    assert empty_array_errors({"interpretation": []}) == [
        "resource.interpretation는 빈 배열일 수 없습니다. 값이 없으면 속성을 제거하세요."
    ]
