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
    iter_cohort_patient_ids,
    resolve_cohort_patient_file,
)


def test_export_request_requires_explicit_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="하나만"):
        ExportRequest(output_directory=tmp_path).validate()
    with pytest.raises(ValueError, match="하나만"):
        ExportRequest(
            output_directory=tmp_path, subject_ids=(1,), limit=1
        ).validate()


def test_demo_patient_identifiers_define_export_cohort(tmp_path: Path) -> None:
    patient_file = tmp_path / "2.1.0" / "fhir" / "MimicPatient.ndjson.gz"
    patient_file.parent.mkdir(parents=True)
    resources = [
        {
            "resourceType": "Patient",
            "id": f"uuid-{subject_id}",
            "identifier": [{"value": str(subject_id)}],
        }
        for subject_id in (10007795, 10007928)
    ]
    with gzip.open(patient_file, "wt", encoding="utf-8") as handle:
        for resource in resources:
            handle.write(json.dumps(resource) + "\n")

    settings = replace(
        load_settings(require_database=False), mimic_fhir_demo_path=tmp_path
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

        def run(self, _subject_id):
            return SimpleNamespace(
                fhir_payload=bundle, verification_errors=[], llm_calls_used=0
            )

    monkeypatch.setattr(
        "fhir_agent_pipeline.export_service.LlmSqlToFhirPipeline", FakePipeline
    )

    result = FhirBatchExportService(settings).run(
        ExportRequest(output_directory=tmp_path, subject_ids=(1,))
    )

    assert result["valid_patients"] == 1
    assert result["cohort"]["source"] == "explicit-subject-ids"
    assert result["resource_counts"] == {"Patient": 1}
    assert (tmp_path / "subject-1.bundle.json").is_file()
    assert (tmp_path / "subject-1.validation.json").is_file()
    assert (tmp_path / "manifest.json").is_file()
