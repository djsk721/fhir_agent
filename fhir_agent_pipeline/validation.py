from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import subprocess
import tempfile
import re
from typing import Any

from .config import Settings
from .fhir_schema import FhirSchemaRepository, get_fhir_schema
from .resource_registry import (
    required_bundle_resource_types,
    resource_semantic_errors,
)


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: list[str]
    warnings: list[str]
    metrics: dict[str, Any]
    external_validator_used: bool
    validator_issue_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reference_metrics(bundle: dict[str, Any]) -> tuple[list[str], float]:
    entries = bundle.get("entry", [])
    targets: set[str] = set()
    for entry in entries:
        resource = entry.get("resource", {})
        full_url = entry.get("fullUrl")
        if full_url:
            targets.add(full_url)
        if resource.get("resourceType") and resource.get("id"):
            targets.add(f"{resource['resourceType']}/{resource['id']}")

    references: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str) and not reference.startswith("#"):
                references.append(reference)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(bundle)
    unresolved = [
        reference
        for reference in references
        if reference not in targets
        and not reference.startswith(("http://", "https://"))
    ]
    score = 1.0 if not references else (len(references) - len(unresolved)) / len(references)
    return [f"Bundle에서 해석할 수 없는 참조: {ref}" for ref in unresolved], score


def _quality_metrics(
    bundle: dict[str, Any],
    required_types: list[str],
) -> dict[str, float | int | bool]:
    resources = [
        entry.get("resource", {})
        for entry in bundle.get("entry", [])
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]
    ids = sum(bool(resource.get("id")) for resource in resources)
    coded = 0
    terminology = 0
    for resource in resources:
        concepts = []
        if resource.get("code"):
            concepts.append(resource["code"])
        categories = resource.get("category", [])
        concepts.extend(categories if isinstance(categories, list) else [])
        for concept in concepts:
            for coding in concept.get("coding", []) if isinstance(concept, dict) else []:
                coded += 1
                terminology += bool(coding.get("system") and coding.get("code"))
    total = len(resources)
    actual = {item.get("resourceType") for item in resources}
    return {
        "resource_count": total,
        "required_resource_coverage": (
            1.0
            if not required_types
            else len(actual.intersection(required_types)) / len(set(required_types))
        ),
        "id_coverage": 1.0 if not total else ids / total,
        "terminology_system_coverage": 1.0 if not coded else terminology / coded,
    }


@dataclass(frozen=True)
class _ExternalValidatorResult:
    used: bool
    errors: list[str]
    warnings: list[str]
    issue_counts: dict[str, int]


_VALIDATOR_SEVERITY = re.compile(
    r":\s*(Fatal|Error|Warning|Information)\s+-", re.IGNORECASE
)


def _validator_issue_counts(output: str) -> dict[str, int]:
    counts = {"fatal": 0, "error": 0, "warning": 0, "information": 0}
    for severity in _VALIDATOR_SEVERITY.findall(output):
        counts[severity.lower()] += 1
    return counts


def _run_external_validator(
    bundle: dict[str, Any],
    jar: Path,
    version: str,
    timeout_seconds: int,
    max_output_chars: int,
    terminology_server: str | None,
) -> _ExternalValidatorResult:
    if not jar.is_file():
        return _ExternalValidatorResult(
            False,
            [f"FHIR_VALIDATOR_JAR 파일을 찾을 수 없습니다: {jar}"],
            [],
            {"fatal": 0, "error": 0, "warning": 0, "information": 0},
        )
    with tempfile.TemporaryDirectory(prefix="fhir-validator-") as directory:
        bundle_path = Path(directory) / "bundle.json"
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        try:
            command = [
                "java",
                "-jar",
                str(jar),
                str(bundle_path),
                "-version",
                version,
                "-output-style",
                "compact",
            ]
            if terminology_server:
                command.extend(["-tx", terminology_server])
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _ExternalValidatorResult(
                True,
                [f"HL7 Validator 시간 초과({timeout_seconds}초): {exc}"],
                [],
                {"fatal": 0, "error": 1, "warning": 0, "information": 0},
            )
    output = "\n".join(part for part in (process.stdout, process.stderr) if part).strip()
    counts = _validator_issue_counts(output)
    retained_output = output[-max_output_chars:]
    errors: list[str] = []
    warnings: list[str] = []
    if process.returncode or counts["fatal"] or counts["error"]:
        errors.append(
            f"HL7 Validator 실패(exit={process.returncode}, issues={counts}): "
            f"{retained_output}"
        )
    elif output:
        warnings.append(retained_output)
    return _ExternalValidatorResult(True, errors, warnings, counts)


def validate_bundle_detailed(bundle: dict[str, Any], settings: Settings) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    bundle_ok = isinstance(bundle, dict) and bundle.get("resourceType") == "Bundle"
    entries = bundle.get("entry", []) if isinstance(bundle, dict) else []
    if not bundle_ok or not isinstance(entries, list):
        errors.append("FHIR 결과는 entry 배열을 가진 Bundle이어야 합니다.")
        entries = []
    actual_types = {
        entry.get("resource", {}).get("resourceType")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    }
    required_types = required_bundle_resource_types(
        settings.fhir_output_resources
    )
    for required_type in required_types:
        if required_type not in actual_types:
            errors.append(f"Bundle에 필수 resourceType {required_type!r}가 없습니다.")

    schema: FhirSchemaRepository | None = None
    if settings.fhir_schema_path.is_file():
        schema = get_fhir_schema(settings.fhir_schema_path)
        bundle_schema_errors = schema.errors(bundle)
        errors.extend(f"Bundle {item}" for item in bundle_schema_errors)
        bundle_ok = bundle_ok and not bundle_schema_errors
    else:
        errors.append(f"FHIR_SCHEMA_PATH 파일을 찾을 수 없습니다: {settings.fhir_schema_path}")

    resources = [
        entry.get("resource", {})
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]
    schema_valid_count = 0
    for index, resource in enumerate(resources):
        schema_errors = schema.errors(resource) if schema else []
        semantic_errors = resource_semantic_errors(resource)
        errors.extend(
            f"entry[{index}] {item}"
            for item in [*schema_errors, *semantic_errors]
        )
        if not schema_errors and not semantic_errors:
            schema_valid_count += 1

    reference_errors, reference_score = _reference_metrics(bundle)
    errors.extend(reference_errors)
    metrics = _quality_metrics(bundle, required_types)
    metrics.update(
        {
            "schema_valid_resource_ratio": (
                1.0 if not resources else schema_valid_count / len(resources)
            ),
            "reference_resolution_ratio": reference_score,
            "bundle_structure_valid": bundle_ok,
        }
    )

    external_used = False
    validator_issue_counts: dict[str, int] = {}
    if settings.fhir_validator_jar:
        external_result = _run_external_validator(
            bundle,
            settings.fhir_validator_jar,
            settings.fhir_version,
            settings.fhir_validator_timeout_seconds,
            settings.fhir_validator_max_output_chars,
            settings.fhir_validator_terminology_server,
        )
        external_used = external_result.used
        validator_issue_counts = external_result.issue_counts
        errors.extend(external_result.errors)
        warnings.extend(external_result.warnings)
    else:
        warnings.append(
            "HL7 Validator는 비활성화됨(FHIR_VALIDATOR_JAR 미설정). "
            "내장 JSON Schema/의미 검증은 실행됨."
        )
    return ValidationReport(
        not errors,
        errors,
        warnings,
        metrics,
        external_used,
        validator_issue_counts,
    )
