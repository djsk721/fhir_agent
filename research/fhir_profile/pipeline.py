from __future__ import annotations

from copy import deepcopy
from collections import Counter
import json
import re
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from fhir_agent_pipeline.fhir_schema import get_fhir_schema
from fhir_agent_pipeline.resource_registry import resource_semantic_errors

from .contracts import SourceBatch
from .profiles.schema import validate_profile
from .renderer.operations import render_records_detailed
from .validation.fidelity import evaluate_fidelity, evaluate_profile_execution_consistency, evaluate_references, evaluate_source_preservation


DirectGenerator = Callable[[SourceBatch], list[dict[str, Any]]]
ProfileReviser = Callable[[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]], Any]
ResourceValidator = Callable[[list[dict[str, Any]]], Any]
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "fhir.schema.json"


def _prf(expected: set[tuple[str, ...]], actual: set[tuple[str, ...]]) -> dict[str, Any]:
    matched = len(expected & actual)
    precision = matched / len(actual) if actual else (1.0 if not expected else 0.0)
    recall = matched / len(expected) if expected else (1.0 if not actual else 0.0)
    return {
        "expected": len(expected), "predicted": len(actual), "matched": matched,
        "false_positive": len(actual - expected), "false_negative": len(expected - actual),
        "precision": precision, "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "accuracy": recall,
    }


def _mapping_atoms(profile: dict[str, Any], *, include_identity: bool = True) -> tuple[set, set, set]:
    fields: set[tuple[str, ...]] = set()
    operations: set[tuple[str, ...]] = set()
    references: set[tuple[str, ...]] = set()
    if include_identity:
        for name in profile.get("identity", {}).get("source_fields", []):
            fields.add((str(name), "id"))
        operations.add(("id", json.dumps(profile.get("identity", {}), sort_keys=True, ensure_ascii=False)))
    for mapping in profile.get("mappings", []):
        target = str(mapping.get("target_path", ""))
        operation = str(mapping.get("operation", ""))
        if mapping.get("source_field"):
            fields.add((str(mapping["source_field"]), target))
        # Include lookup tables, constants and nested operations, excluding labels only.
        configuration = {key: value for key, value in mapping.items() if key not in {"id", "target_path", "description"}}
        operations.add((target, json.dumps(configuration, sort_keys=True, ensure_ascii=False)))
        if operation == "reference":
            reference_identity = mapping.get("identity") or {"source_fields": [mapping.get("source_field", "")]}
            normalized_identity = {
                "source_fields": reference_identity["source_fields"],
                "separator": reference_identity.get("separator", "-"),
                "prefix": reference_identity.get("prefix", ""),
            }
            for name in reference_identity["source_fields"]:
                fields.add((str(name), target))
            references.add((json.dumps(normalized_identity, sort_keys=True), str(mapping.get("resource_type", "")), target))
        for name in ("system", "code", "display", "value"):
            part = mapping.get(name)
            if not isinstance(part, dict) or not part.get("source_field"):
                continue
            if operation == "codeable_concept":
                nested_path = f"{target}.coding.0.{name}"
            elif operation == "identifier":
                nested_path = f"{target}.0.{name}"
            else:
                nested_path = f"{target}.{name}"
            fields.add((str(part["source_field"]), nested_path))
    return fields, operations, references


def mapping_accuracy(predicted: dict[str, Any], reference: dict[str, Any], *, include_identity: bool = True) -> dict[str, Any]:
    """독립 참조 Profile 대비 매핑 Precision/Recall/F1 (추가 오매핑 감점)."""
    actual_fields, actual_operations, actual_references = _mapping_atoms(predicted, include_identity=include_identity)
    expected_fields, expected_operations, expected_references = _mapping_atoms(reference, include_identity=include_identity)
    return {
        "resource_mapping_accuracy": float(predicted.get("target_resource") == reference.get("target_resource")),
        "field_mapping_accuracy": _prf(expected_fields, actual_fields),
        "datatype_mapping_accuracy": _prf(expected_operations, actual_operations),
        "reference_mapping_accuracy": _prf(expected_references, actual_references),
    }


def _blocking(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if issue.get("severity", "error") not in {"warning", "information"}]


def issue_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """동일 오류의 중복 수까지 보존해 해결·신규 오류를 계산합니다."""
    def signature(issue):
        return json.dumps({key: value for key, value in issue.items() if key not in {"mapping_id", "actual_value"}}, sort_keys=True, ensure_ascii=False)
    old = Counter(signature(issue) for issue in _blocking(before))
    new = Counter(signature(issue) for issue in _blocking(after))
    return {
        "resolved": [json.loads(key) for key, count in (old - new).items() for _ in range(count)],
        "introduced": [json.loads(key) for key, count in (new - old).items() for _ in range(count)],
        "remaining": sum((old & new).values()),
    }


def _source_fields(profile: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("source_field"):
                result.add(str(value["source_field"]))
            result.update(str(field) for field in value.get("source_fields", []))
            if "field" in value:
                result.add(str(value["field"]))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(profile.get("mappings", []))
    return result


class HybridFhirPipeline:
    """Profile 실행·검증 및 선택적 LLM Profile 교정. 정답은 교정에 노출하지 않습니다."""

    def __init__(
        self,
        *,
        correct: bool = False,
        max_iterations: int = 2,
        timezone: str | None = None,
        profile_reviser: ProfileReviser | None = None,
        validator: ResourceValidator | None = None,
        schema_path: str | Path = DEFAULT_SCHEMA_PATH,
        source_checks: list[dict[str, Any]] | None = None,
    ) -> None:
        if max_iterations < 0:
            raise ValueError("max_iterations는 0 이상이어야 합니다.")
        self.correct = correct
        self.max_iterations = max_iterations
        self.timezone = timezone
        self.profile_reviser = profile_reviser
        self.validator = validator
        self.schema_path = Path(schema_path)
        self.source_checks = deepcopy(source_checks or [])

    def _assess(
        self,
        profile: dict[str, Any],
        records: list[dict[str, Any]],
        reference_resources: list[dict[str, Any]] | None,
        validator: ResourceValidator | None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        rendered = render_records_detailed(profile, records, self.timezone, metadata=metadata, schema_path=self.schema_path)
        resources, render_errors = rendered.resources, rendered.errors
        consistency = evaluate_profile_execution_consistency(profile, records, resources, self.timezone, source_indices=rendered.source_indices)
        issues: list[dict[str, Any]] = [
            {"category": "render", "mapping_id": "", "message": message} for message in render_errors
        ]
        schema = get_fhir_schema(self.schema_path)
        for index, resource in enumerate(resources):
            for message in schema.errors(resource):
                issues.append({"category": "json_schema", "mapping_id": "", "message": f"record[{index}] {message}"})
            for message in resource_semantic_errors(resource):
                issues.append({"category": "semantic", "mapping_id": "", "message": f"record[{index}] {message}"})
        reference_result = evaluate_references(resources, reference_resources)
        issues.extend(reference_result["issues"])
        issues.extend(consistency["issues"])
        preservation = evaluate_source_preservation(records, resources, self.source_checks, source_indices=rendered.source_indices)
        issues.extend(preservation["issues"])
        external: dict[str, Any] = {"status": "not_evaluated", "external_validator_used": False}
        if validator is not None:
            raw = validator(deepcopy(resources))
            external = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw)
            external.setdefault("status", "evaluated")
            if external.get("issues"):
                issues.extend(external["issues"])
                messages = external.get("local_errors", [])
            elif external.get("external_validation_status") in {"timeout", "unavailable", "execution_failed"}:
                issues.append({"category": "validator_execution", "mapping_id": "",
                               "message": f"HL7 Validator {external['external_validation_status']}"})
                messages = external.get("local_errors", [])
            else:
                messages = external.get("errors", [])
            # The bundle validator repeats the per-record schema and semantic checks.
            local_counts = Counter(re.sub(r"^record\[\d+\] ", "", issue["message"])
                                   for issue in issues if issue["category"] in {"json_schema", "semantic"})
            for message in messages:
                if local_counts[message]:
                    local_counts[message] -= 1
                else:
                    issues.append({"category": "validator", "mapping_id": "", "message": str(message)})
            for message in external.get("warnings", []):
                issues.append({"category": "validator", "mapping_id": "", "severity": "warning", "message": str(message)})
            if external.get("valid") is False and not external.get("errors"):
                issues.append({"category": "validator", "mapping_id": "", "message": "추가 Validator가 invalid를 반환했습니다."})
        for issue in issues:
            if issue.get("mapping_id"):
                continue
            for mapping in profile["mappings"]:
                if str(mapping["target_path"]) in issue.get("message", ""):
                    issue["mapping_id"] = mapping["id"]
                    break
        validation = {
            "valid": not _blocking(issues),
            "json_schema_used": True,
            "source_indices": rendered.source_indices,
            "source_preservation": preservation,
            "external": external,
            "reference_resolution": {key: value for key, value in reference_result.items() if key != "issues"},
        }
        return resources, consistency, validation, issues

    def run(
        self,
        batch: SourceBatch,
        profile: dict[str, Any],
        *,
        expected_resources: list[dict[str, Any]] | None = None,
        reference_resources: list[dict[str, Any]] | None = None,
        validator: ResourceValidator | None = None,
        match_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
        field_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        current = validate_profile(profile, batch.metadata, schema_path=self.schema_path)
        if self.source_checks:
            check_profile = {**current, "mappings": self.source_checks}
            validate_profile(check_profile, batch.metadata, schema_path=self.schema_path)
        if current["source"]["table"] != batch.source_resource:
            raise ValueError("Profile source.table과 SourceBatch.source_resource가 다릅니다.")
        records = [dict(row) for row in batch.records]
        history: list[dict[str, Any]] = []
        attempts = accepted = 0
        active_validator = validator or self.validator
        resources, consistency, validation, issues = self._assess(current, records, reference_resources, active_validator, batch.metadata)
        first_resources = deepcopy(resources)
        first_issues = deepcopy(issues)
        first_preservation = deepcopy(validation["source_preservation"])
        first_valid = validation["valid"]
        first_consistency = consistency["source_field_accuracy"]["accuracy"]
        revision_feedback: list[dict[str, Any]] = []
        for attempt in range(self.max_iterations + 1):
            history.append({
                "attempt": attempt, "profile": deepcopy(current), "issue_count": len(_blocking(issues)),
                "profile_execution_consistency": consistency["source_field_accuracy"]["accuracy"], "changes": [],
            })
            if not self.correct or not _blocking(issues) or attempt == self.max_iterations or self.profile_reviser is None:
                break
            actionable = [issue for issue in _blocking(issues) if issue["category"] != "validator_execution"]
            if not actionable:
                break
            attempts += 1
            # No independent gold, expected values, or gold-derived issues reach this callback.
            try:
                raw = self.profile_reviser(deepcopy(current), deepcopy([*actionable, *revision_feedback]), deepcopy(records))
                if isinstance(raw, tuple):
                    revised, changes = raw
                else:
                    revised, changes = raw, [{"mapping_id": "", "change": "LLM Profile revision"}]
                revised = validate_profile(revised, batch.metadata, schema_path=self.schema_path)
            except (ValueError, TypeError, KeyError, RuntimeError) as exc:
                history[-1]["changes"] = [{"mapping_id": "", "change": f"rejected: {type(exc).__name__}: {exc}"}]
                revision_feedback = [{"category": "revision_contract", "mapping_id": "", "message": str(exc),
                                      "draft": deepcopy(getattr(exc, "draft", None))}]
                continue
            if revised == current:
                rejection = "unchanged revision; resolve at least one existing blocking issue"
            elif revised["source"] != current["source"] or revised["target_resource"] != current["target_resource"] or revised["identity"] != current["identity"]:
                rejection = "source, target or identity changed"
            else:
                rejection = ""
            lost_fields = _source_fields(current) - _source_fields(revised)
            failed_check_paths = {issue.get("path") for issue in issues if issue.get("category") == "source_preservation"}
            replaceable_fields = set()
            for mapping in current["mappings"]:
                if mapping["target_path"] in failed_check_paths:
                    replaceable_fields.update(_source_fields({"mappings": [mapping]}))
            unapproved_losses = lost_fields - replaceable_fields
            if not rejection and unapproved_losses:
                rejection = f"source fields removed: {sorted(unapproved_losses)}"
            if rejection:
                history[-1]["changes"] = [{"mapping_id": "", "change": f"rejected: {rejection}"}]
                revision_feedback = [{"category": "revision_contract", "mapping_id": "", "message": rejection}]
                continue
            candidate = self._assess(revised, records, reference_resources, active_validator, batch.metadata)
            new_resources, new_consistency, new_validation, new_issues = candidate
            delta = issue_changes(issues, new_issues)
            history[-1]["candidate_issue_changes"] = delta
            if delta["introduced"]:
                rejection = "new blocking errors introduced"
            elif len(_blocking(new_issues)) >= len(_blocking(issues)):
                rejection = "blocking errors did not decrease"
            if rejection:
                history[-1]["changes"] = [{"mapping_id": "", "change": f"rejected: {rejection}"}]
                revision_feedback = [{"category": "revision_contract", "mapping_id": "", "message": rejection,
                                      "draft": deepcopy(revised), "introduced": delta["introduced"][:5]}]
                continue
            current = revised
            revision_feedback = []
            resources, consistency, validation, issues = new_resources, new_consistency, new_validation, new_issues
            history[-1]["changes"] = changes
            accepted += 1
        fidelity = evaluate_fidelity(
            current, records, resources, self.timezone,
            expected_resources=expected_resources, reference_resources=reference_resources,
            match_paths=match_paths, field_paths=field_paths,
        )
        initial_fidelity = evaluate_fidelity(
            current, records, first_resources, self.timezone,
            expected_resources=expected_resources, reference_resources=reference_resources,
            match_paths=match_paths, field_paths=field_paths,
        )
        # Gold is evaluated only after the profile is frozen; it never guides revision selection.
        final_issues = [*issues, *[issue for issue in fidelity["issues"] if issue["category"] == "independent_fidelity"]]
        final_valid = not _blocking(final_issues)
        final_consistency = consistency["source_field_accuracy"]["accuracy"]
        delta = issue_changes(first_issues, issues)
        initial_error_count = len(_blocking(first_issues))
        fidelity_before = initial_fidelity["source_field_accuracy"]["accuracy"]
        fidelity_after = fidelity["source_field_accuracy"]["accuracy"]
        fidelity_preserved = None
        if fidelity_before is not None and fidelity_after is not None:
            fidelity_preserved = (
                fidelity_after >= fidelity_before
                and fidelity["event_detection"]["f1"] >= initial_fidelity["event_detection"]["f1"]
            )
        return {
            "profile": current, "resources": resources, "fidelity": fidelity,
            "source_indices": validation["source_indices"],
            "profile_execution_consistency": consistency, "validation": validation,
            "issues": final_issues, "history": history,
            "metrics": {
                "first_pass_valid": first_valid, "standard_valid": validation["valid"], "final_valid": final_valid,
                "fidelity_evaluated": fidelity["status"] == "evaluated",
                "repair_attempts": attempts, "accepted_revisions": accepted,
                "initial_error_count": initial_error_count,
                "resolved_error_count": len(delta["resolved"]),
                "introduced_error_count": len(delta["introduced"]),
                "proposed_introduced_error_count": sum(len(step.get("candidate_issue_changes", {}).get("introduced", [])) for step in history),
                "error_repair_rate": len(delta["resolved"]) / initial_error_count if initial_error_count else None,
                "source_preservation_before": first_preservation,
                "source_preservation_after": validation["source_preservation"],
                "standard_repair_success": bool(accepted) and validation["valid"] and not first_valid,
                "repair_success": bool(accepted) and final_valid and not first_valid,
                "fidelity_before": fidelity_before, "fidelity_after": fidelity_after,
                "fidelity_preserved": fidelity_preserved,
                "profile_execution_consistency_before": first_consistency,
                "profile_execution_consistency_after": final_consistency,
                "elapsed_seconds": perf_counter() - started,
                "profile_revision_calls": attempts,
                "llm_calls": None,
            },
        }


def run_comparison(
    batch: SourceBatch,
    profile: dict[str, Any],
    *,
    direct_generator: DirectGenerator | None = None,
    reference_profile: dict[str, Any] | None = None,
    max_iterations: int = 2,
    profile_reviser: ProfileReviser | None = None,
    expected_resources: list[dict[str, Any]] | None = None,
    reference_resources: list[dict[str, Any]] | None = None,
    validator: ResourceValidator | None = None,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    match_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
    field_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """동일 원천 데이터 비교. 교정용 데이터와 평가용 gold는 호출자에서 분리합니다."""
    options = {"expected_resources": expected_resources, "reference_resources": reference_resources, "match_paths": match_paths, "field_paths": field_paths}
    hybrid = HybridFhirPipeline(validator=validator, schema_path=schema_path).run(batch, profile, **options)
    corrected = HybridFhirPipeline(correct=True, max_iterations=max_iterations, profile_reviser=profile_reviser, validator=validator, schema_path=schema_path).run(batch, profile, **options)
    groups = {"P1": _group_metrics(hybrid), "P2": _group_metrics(corrected)}
    if direct_generator is not None:
        started = perf_counter()
        resources = direct_generator(batch)
        fidelity = evaluate_fidelity(profile, list(batch.records), resources, **options)
        schema = get_fhir_schema(schema_path)
        errors = [message for resource in resources for message in schema.errors(resource)]
        reference = evaluate_references(resources, reference_resources)
        external = validator(resources) if validator is not None else None
        external = external.as_dict() if hasattr(external, "as_dict") else external
        groups["B1"] = {
            "resource_count": len(resources), "source_field_accuracy": fidelity["source_field_accuracy"]["accuracy"],
            "reference_accuracy": reference["accuracy"],
            "final_valid": not errors and not fidelity["issues"] and (external is None or external.get("valid", True)),
            "elapsed_seconds": perf_counter() - started, "llm_calls": None,
        }
    return {
        "dataset": batch.dataset, "source_resource": batch.source_resource, "record_count": len(batch.records),
        "rq1_mapping_accuracy": mapping_accuracy(profile, reference_profile) if reference_profile is not None else None,
        "groups": groups,
    }


def _group_metrics(result: dict[str, Any]) -> dict[str, Any]:
    fidelity, metrics = result["fidelity"], result["metrics"]
    return {
        "error_free_validation_rate": float(metrics["standard_valid"]),
        "warning_count": sum(issue.get("severity") == "warning" for issue in result["issues"]),
        "source_field_accuracy": fidelity["source_field_accuracy"]["accuracy"],
        "reference_accuracy": fidelity["reference_accuracy"]["accuracy"],
        "resource_count_alignment": fidelity["resource_count_alignment"]["alignment_rate"],
        "profile_execution_consistency": result["profile_execution_consistency"]["source_field_accuracy"]["accuracy"],
        **metrics,
    }
