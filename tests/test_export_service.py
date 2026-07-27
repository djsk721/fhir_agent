from dataclasses import replace
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.export_service import (
    ExportRequest,
    FhirBatchExportService,
    _aggregate,
    iter_cohort_patient_ids,
    resolve_cohort_patient_file,
)


def test_export_request_requires_explicit_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="하나만"):
        ExportRequest(output_directory=tmp_path).validate()
    with pytest.raises(ValueError, match="하나만"):
        ExportRequest(
            output_directory=tmp_path, patient_keys=(1,), limit=1
        ).validate()


def test_manifest_reports_queries_that_reached_row_limit(
    tmp_path: Path,
) -> None:
    summary = _aggregate(
        [
            {
                "status": "valid",
                "sql_generation_metrics": {
                    "row_limit_reached_queries": 2,
                },
            },
            {
                "status": "valid",
                "task_evaluation": {
                    "sql": {"row_limit_reached_queries": 0},
                },
            },
        ],
        tmp_path,
    )

    assert summary["extraction_completeness"] == {
        "row_limit_reached_queries": 2,
        "patients_reaching_row_limit": 1,
        "requires_limit_review": True,
    }


def test_demo_patient_identifiers_define_export_cohort(tmp_path: Path) -> None:
    patient_file = tmp_path / "2.1.0" / "fhir" / "MimicPatient.ndjson.gz"
    patient_file.parent.mkdir(parents=True)
    resources = [
        {
            "resourceType": "Patient",
            "id": f"uuid-{patient_key}",
            "identifier": [
                {
                    "system": (
                        "http://mimic.mit.edu/fhir/mimic/identifier/patient"
                    ),
                    "value": str(patient_key),
                }
            ],
        }
        for patient_key in (10007795, 10007928)
    ]
    with gzip.open(patient_file, "wt", encoding="utf-8") as handle:
        for resource in resources:
            handle.write(json.dumps(resource) + "\n")

    settings = replace(
        load_settings(require_database=False), source_data_path=tmp_path
    )
    assert resolve_cohort_patient_file(settings) == patient_file
    assert list(iter_cohort_patient_ids(settings, limit=None)) == [10007795, 10007928]
    assert list(iter_cohort_patient_ids(settings, limit=1)) == [10007795]


def test_batch_export_persists_then_validates(monkeypatch, tmp_path: Path) -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_export_directory=tmp_path,
        fhir_validator_jar=None,
        fhir_output_resources=["Patient"],
    )

    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "urn:resource:patient:p1",
                "resource": {"resourceType": "Patient", "id": "p1"},
            }
        ],
    }

    class FakePipeline:
        def __init__(self, _settings):
            pass

        def run(self, _patient_key):
            return SimpleNamespace(
                fhir_payload=bundle, verification_errors=[], llm_calls_used=0
            )

    monkeypatch.setattr(
        "fhir_agent_pipeline.export_service.LlmSqlToFhirPipeline", FakePipeline
    )

    result = FhirBatchExportService(settings).run(
        ExportRequest(output_directory=tmp_path, patient_keys=(1,))
    )

    assert result["valid_patients"] == 1
    assert result["cohort"]["source"] == "explicit-patient-ids"
    assert result["resource_counts"] == {"Patient": 1}
    assert (tmp_path / "patient-1.bundle.json").is_file()
    assert (tmp_path / "patient-1.validation.json").is_file()
    assert (tmp_path / "manifest.json").is_file()


def test_resume_regenerates_invalid_result(monkeypatch, tmp_path: Path) -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_export_directory=tmp_path,
        fhir_validator_jar=None,
        fhir_output_resources=["Patient"],
    )
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{
            "fullUrl": "urn:resource:patient:p1",
            "resource": {"resourceType": "Patient", "id": "p1"},
        }],
    }
    (tmp_path / "patient-1.bundle.json").write_text(
        json.dumps(bundle), encoding="utf-8"
    )
    (tmp_path / "patient-1.validation.json").write_text(
        json.dumps({"valid": False}), encoding="utf-8"
    )
    calls: list[int] = []

    class FakePipeline:
        def __init__(self, _settings):
            pass

        def run(self, patient_key):
            calls.append(patient_key)
            return SimpleNamespace(
                fhir_payload=bundle, verification_errors=[], llm_calls_used=0
            )

    monkeypatch.setattr(
        "fhir_agent_pipeline.export_service.LlmSqlToFhirPipeline", FakePipeline
    )

    result = FhirBatchExportService(settings).run(
        ExportRequest(
            output_directory=tmp_path,
            patient_keys=(1,),
            resume=True,
        )
    )

    assert calls == [1]
    assert result["valid_patients"] == 1
    assert result["resumed_patients"] == 0


def test_resume_regenerates_failed_task_evaluation(
    monkeypatch, tmp_path: Path
) -> None:
    settings = replace(
        load_settings(require_database=False),
        fhir_export_directory=tmp_path,
        fhir_validator_jar=None,
        fhir_output_resources=["Patient"],
    )
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{
            "fullUrl": "urn:resource:patient:p1",
            "resource": {"resourceType": "Patient", "id": "p1"},
        }],
    }
    (tmp_path / "patient-1.bundle.json").write_text(
        json.dumps(bundle), encoding="utf-8"
    )
    (tmp_path / "patient-1.validation.json").write_text(
        json.dumps({
            "valid": True,
            "metrics": {
                "task_evaluation": {"task_quality_pass": False},
            },
        }),
        encoding="utf-8",
    )
    calls: list[int] = []

    class FakePipeline:
        def __init__(self, _settings):
            pass

        def run(self, patient_key):
            calls.append(patient_key)
            return SimpleNamespace(
                fhir_payload=bundle, verification_errors=[], llm_calls_used=0
            )

    monkeypatch.setattr(
        "fhir_agent_pipeline.export_service.LlmSqlToFhirPipeline", FakePipeline
    )

    FhirBatchExportService(settings).run(
        ExportRequest(
            output_directory=tmp_path,
            patient_keys=(1,),
            resume=True,
        )
    )

    assert calls == [1]
