from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from research.fhir_profile.fhir_constraints import FhirConstraintLookup


CANONICAL = "http://hl7.org/fhir/StructureDefinition/Example"
REFERENCE = "http://hl7.org/fhir/StructureDefinition/Encounter"


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def snapshot() -> dict:
    return {
        "resourceType": "StructureDefinition",
        "kind": "resource",
        "type": "Example",
        "url": CANONICAL,
        "fhirVersion": "4.0.1",
        "snapshot": {
            "element": [
                {"path": "Example", "min": 0, "max": "*"},
                {
                    "path": "Example.link",
                    "min": 1,
                    "max": "1",
                    "type": [{"code": "Reference", "targetProfile": [REFERENCE]}],
                },
                {
                    "path": "Example.value[x]",
                    "min": 0,
                    "max": "1",
                    "type": [{"code": "string"}, {"code": "Quantity"}],
                },
                {
                    "path": "Example.component",
                    "min": 0,
                    "max": "*",
                    "type": [{"code": "BackboneElement"}],
                },
                {
                    "path": "Example.component.status",
                    "min": 0,
                    "max": "1",
                    "type": [{"code": "code"}],
                    "binding": {"strength": "required", "valueSet": "https://example.test/ValueSet/status"},
                    "constraint": [
                        {"source": CANONICAL, "key": "ex-1", "severity": "error", "expression": "exists()"},
                        {"source": REFERENCE, "key": "inherited-1", "severity": "warning", "expression": "exists()"},
                    ],
                },
                {
                    "path": "Example.component.disabled",
                    "min": 0,
                    "max": "0",
                },
            ]
        },
    }


def _package(tmp_path: Path, snapshot: dict, version: str = "4.0.1") -> Path:
    package = tmp_path / "package"
    package.mkdir(parents=True)
    _write(package / "package.json", {"name": "example.core", "version": version, "fhirVersions": [version]})
    _write(package / "StructureDefinition-Example.json", snapshot)
    return package


def test_exact_constraint_context_preserves_paths_choices_refs_and_binding(tmp_path, snapshot):
    package = _package(tmp_path, snapshot)
    lookup = FhirConstraintLookup(package.parent)
    context = json.loads(lookup.context("Example"))
    assert context["canonical"] == CANONICAL
    elements = {entry["path"]: entry for entry in context["elements"]}
    assert elements["Example.link"] == {
        "path": "Example.link",
        "min": 1,
        "max": "1",
        "types": [{"code": "Reference", "targetProfile": [REFERENCE]}],
    }
    assert elements["Example.value[x]"]["max"] == "1"
    assert elements["Example.value[x]"]["types"] == [
        {"code": "string", "json_path": "Example.valueString"},
        {"code": "Quantity", "json_path": "Example.valueQuantity"},
    ]
    assert elements["Example.component"]["max"] == "*"
    assert elements["Example.component.disabled"]["max"] == "0"
    assert elements["Example.component.status"]["binding"] == {
        "strength": "required",
        "valueSet": "https://example.test/ValueSet/status",
    }
    assert [rule["key"] for rule in elements["Example.component.status"]["constraints"]] == ["ex-1"]
    assert FhirConstraintLookup(package).context("Example") == lookup.context("Example")


def test_package_identity_matches_content_and_fails_on_drift(tmp_path, snapshot):
    old = _package(tmp_path / "a", snapshot)
    copy = _package(tmp_path / "b", snapshot)
    lookup = FhirConstraintLookup(old)
    assert lookup.identity == FhirConstraintLookup(copy).identity
    modified = deepcopy(snapshot)
    modified["snapshot"]["element"][1]["max"] = "*"
    _write(old / "StructureDefinition-Example.json", modified)
    assert lookup.identity != FhirConstraintLookup(old).identity
    with pytest.raises(ValueError, match="조회 중 변경"):
        lookup.context("Example")


def test_missing_or_mismatched_snapshot_never_falls_back(tmp_path, snapshot):
    package = _package(tmp_path, snapshot)
    with pytest.raises(ValueError, match="형식"):
        FhirConstraintLookup(package).context("../Example")
    with pytest.raises(ValueError, match="없습니다"):
        FhirConstraintLookup(package).context("Unknown")
    with pytest.raises(ValueError, match="FHIR 버전"):
        FhirConstraintLookup(_package(tmp_path / "other", snapshot, version="5.0.0"))
    snapshot.pop("snapshot")
    _write(package / "StructureDefinition-Example.json", snapshot)
    with pytest.raises(ValueError, match="snapshot"):
        FhirConstraintLookup(package).context("Example")
