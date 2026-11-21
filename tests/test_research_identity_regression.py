from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from research.fhir_profile.profiles.schema import bind_profile_identity
from research.fhir_profile.renderer.operations import render_records_detailed, validate_record_identity
from research.fhir_profile.source_rag import source_metadata
from research.fhir_profile.validation.fidelity import evaluate_references


def test_source_identity_recovers_rows_without_optional_reference_and_is_order_stable() -> None:
    metadata = {
        "columns": ["person", "visit", "reading"],
        "record_identity_fields": ["row_locator"],
        "identity_contract": True,
    }
    model_profile = {
        "profile_id": "fixture",
        "profile_version": "1",
        "source": {"dataset": "fixture", "table": "readings"},
        "target_resource": "Observation",
        "fhir_version": "4.0.1",
        "identity": {"source_fields": ["person", "visit", "reading"]},
        "mappings": [
            {"id": "reading", "target_path": "valueString", "operation": "string", "source_field": "reading"},
            {"id": "visit", "target_path": "encounter", "operation": "reference", "resource_type": "Encounter",
             "source_field": "visit"},
        ],
    }
    rows = [
        {"person": "p1", "visit": "", "reading": "same", "row_locator": "readings.csv:2"},
        {"person": "p1", "visit": None, "reading": "same", "row_locator": "readings.csv:3"},
        {"person": "p1", "visit": "v1", "reading": "same", "row_locator": "readings.csv:4"},
    ]
    original = deepcopy(model_profile)
    bound = bind_profile_identity(model_profile, metadata)
    assert model_profile == original
    assert bound["identity"] == {"source_fields": ["row_locator"]}
    assert bound["mappings"] == original["mappings"]
    validate_record_identity(rows, metadata)
    result = render_records_detailed(bound, rows, metadata=metadata)
    assert result.errors == []
    assert result.source_indices == [0, 1, 2]
    assert len({resource["id"] for resource in result.resources}) == 3
    assert all("encounter" not in resource for resource in result.resources[:2])
    assert result.resources[2]["encounter"] == {"reference": "Encounter/v1"}
    assert all("row_locator" not in resource for resource in result.resources)
    targets = [{"resourceType": "Encounter", "id": "v1"}]
    assert evaluate_references(result.resources, targets)["issues"] == []
    assert [issue["category"] for issue in evaluate_references(result.resources, [])["issues"]] == ["reference_unresolved"]

    reversed_result = render_records_detailed(bound, list(reversed(rows)), metadata=metadata)
    assert reversed_result.resources == list(reversed(result.resources))
    with pytest.raises(ValueError):
        validate_record_identity([rows[0], rows[0]], metadata)
    with pytest.raises(ValueError):
        validate_record_identity([{"reading": "same", "visit": "v1"}], metadata)


def test_fixed_synthea_observations_replay_without_adjusting_clinical_mapping() -> None:
    # These files are deliberately git-ignored. CI can exercise the synthetic regression above;
    # the local research snapshot additionally proves what caused the original 42 row losses.
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    snapshot = artifacts / "fixed-pilot-20261121" / "synthea"
    paths = {
        "snapshot": snapshot / "test.json",
        "spec": snapshot / "spec.json",
        "archived": artifacts / "dual-rag-baseline-20261121" / "synthea" / "dual.profiles.json",
    }
    if not all(path.is_file() for path in paths.values()):
        pytest.skip("고정 Synthea 연구 산출물이 없어 실제 스냅샷 재현을 생략합니다.")
    values = {key: json.loads(path.read_text(encoding="utf-8")) for key, path in paths.items()}
    batch = values["snapshot"]["resources"]["Observation"]
    records = batch["records"]
    old = values["archived"]["profiles"]["Observation"]["corrected"]
    historical = render_records_detailed(old, records)
    assert len(historical.resources) == 241
    assert len(historical.errors) == 42

    metadata = {
        **source_metadata(values["spec"], "Observation"),
        "record_identity_fields": batch["metadata"]["record_identity_fields"],
        "identity_contract": True,
    }
    bound = bind_profile_identity(old, metadata)
    # Confirm only the identity contract changed; do not disguise changed clinical semantics.
    assert bound["mappings"] == old["mappings"]
    validate_record_identity(records, metadata)
    replay = render_records_detailed(bound, records, metadata=metadata)
    assert replay.source_indices == list(range(len(records)))
    assert len(replay.resources) == len({resource["id"] for resource in replay.resources}) == 283
    visit_column = values["spec"]["resources"]["Observation"]["encounter_column"]
    assert len([resource for resource in replay.resources if "encounter" not in resource]) == 42
    assert all(("encounter" in resource) == bool(row[visit_column])
               for row, resource in zip(records, replay.resources, strict=True))
    assert not replay.errors
