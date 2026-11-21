from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from typing import Any

from ..renderer.operations import OMIT, apply_mapping, resource_id, set_path, prune, get_path


def _score(correct: int, evaluated: int) -> dict[str, Any]:
    return {
        "status": "evaluated" if evaluated else "not_evaluated",
        "correct": correct,
        "evaluated": evaluated,
        "accuracy": correct / evaluated if evaluated else None,
    }


def _expected_reference_metrics(expected: int, present: int, correct: int) -> dict[str, Any]:
    return {
        "status": "evaluated" if expected else "not_evaluated",
        "expected": expected,
        "present": present,
        "correct": correct,
        "missing": expected - present,
        "wrong_target": present - correct,
        "coverage": _score(present, expected),
        "target_accuracy": _score(correct, expected),
        "present_target_accuracy": _score(correct, present),
    }


def _leaves(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for name, nested in value.items():
            result.update(_leaves(nested, f"{path}.{name}" if path else name))
        return result
    if isinstance(value, list):
        result = {}
        for index, nested in enumerate(value):
            result.update(_leaves(nested, f"{path}.{index}"))
        return result
    return {path: value}


def _identity(resource: dict[str, Any]) -> str:
    return f"{resource.get('resourceType', '')}/{resource.get('id', '')}"


def _canonical_value(value: Any, path: str) -> Any:
    """표준적으로 동등한 숫자·timezone 표기만 정규화합니다."""
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            return ["invalid_number", str(number)]
        sign, digits, exponent = number.as_tuple()
        reduced = list(digits)
        while len(reduced) > 1 and reduced[-1] == 0:
            reduced.pop()
            exponent += 1
        if not any(reduced):
            sign, exponent = 0, 0
        return ["number", sign, reduced, exponent]
    field = path.rsplit(".", 1)[-1]
    datetime_field = field.endswith(("DateTime", "Instant")) or field in {
        "start", "end", "issued", "recordedDate", "date",
    }
    if isinstance(value, str) and datetime_field and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:0\d|1[0-3]):[0-5]\d|[+-]14:00)", value,
    ):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ["datetime", timestamp.astimezone(timezone.utc).isoformat()]
        except ValueError:
            pass
    return [type(value).__name__, value]


def _equivalent(actual: Any, expected: Any, path: str) -> bool:
    return _canonical_value(actual, path) == _canonical_value(expected, path)


def _detection(expected: Counter[str], actual: Counter[str]) -> dict[str, Any]:
    matched = sum((expected & actual).values())
    expected_count, generated_count = sum(expected.values()), sum(actual.values())
    precision = matched / generated_count if generated_count else (1.0 if not expected_count else 0.0)
    recall = matched / expected_count if expected_count else (1.0 if not generated_count else 0.0)
    return {
        "expected": expected_count,
        "generated": generated_count,
        "matched": matched,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "alignment_rate": (
            min(expected_count, generated_count) / max(expected_count, generated_count)
            if max(expected_count, generated_count) else 1.0
        ),
    }


def evaluate_references(
    resources: list[dict[str, Any]],
    reference_resources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """평가 범위의 실제 ID만 사용합니다. 범위가 없으면 점수를 만들지 않습니다."""
    if reference_resources is None:
        return {**_score(0, 0), "issues": []}
    targets: set[str] = set()
    by_reference: dict[str, dict[str, Any]] = {}
    for item in [*reference_resources, *resources]:
        resource = item.get("resource", item)
        if resource.get("resourceType") and resource.get("id"):
            targets.add(_identity(resource))
            by_reference[_identity(resource)] = resource
        if item.get("fullUrl"):
            targets.add(str(item["fullUrl"]))
            by_reference[str(item["fullUrl"])] = resource
    correct = evaluated = 0
    issues: list[dict[str, Any]] = []

    def visit(value: Any, owner: dict[str, Any], path: str) -> None:
        nonlocal correct, evaluated
        if isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str):
                evaluated += 1
                contained = {
                    f"#{entry.get('id')}" for entry in owner.get("contained", [])
                    if isinstance(entry, dict) and entry.get("id")
                }
                if reference in targets or reference in contained:
                    correct += 1
                else:
                    issues.append({
                        "category": "reference_unresolved", "mapping_id": "",
                        "message": f"{_identity(owner)} {path}.reference 대상이 없습니다: {reference}",
                    })
            for key, nested in value.items():
                visit(nested, owner, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, owner, f"{path}.{index}")

    for resource in resources:
        visit(resource, resource, "")
        subject = get_path(resource, "subject.reference")
        encounter_ref = get_path(resource, "encounter.reference")
        encounter = by_reference.get(encounter_ref) if isinstance(encounter_ref, str) else None
        if isinstance(subject, str) and encounter and encounter.get("resourceType") == "Encounter":
            owner_ref = get_path(encounter, "subject.reference")
            if isinstance(owner_ref, str):
                evaluated += 1
                subject_target = by_reference.get(subject)
                owner_target = by_reference.get(owner_ref)
                same_patient = (
                    _identity(subject_target) == _identity(owner_target)
                    if subject_target is not None and owner_target is not None else subject == owner_ref
                )
                if same_patient:
                    correct += 1
                else:
                    issues.append({"category": "reference_wrong_patient", "mapping_id": "",
                                   "message": f"{_identity(resource)} subject와 Encounter의 Patient가 다릅니다."})
    return {**_score(correct, evaluated), "issues": issues}


def _compare_resources(
    expected_resources: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    *,
    category: str,
) -> dict[str, Any]:
    expected_counts = Counter(_identity(item) for item in expected_resources)
    actual_counts = Counter(_identity(item) for item in resources)
    actual_by_id = {_identity(item): item for item in resources}
    issues: list[dict[str, Any]] = []
    correct = evaluated = 0
    missing = object()
    for expected in expected_resources:
        identity = _identity(expected)
        actual = actual_by_id.get(identity)
        if actual is None:
            issues.append({"category": category, "mapping_id": "", "message": f"기대 리소스가 없습니다: {identity}"})
        expected_fields = _leaves(expected)
        actual_fields = _leaves(actual or {})
        for path, expected_value in expected_fields.items():
            if path in {"resourceType", "id"}:
                continue
            evaluated += 1
            actual_value = actual_fields.get(path, missing)
            if _equivalent(actual_value, expected_value, path):
                correct += 1
            else:
                issues.append({
                    "category": category, "mapping_id": "", "path": path,
                    "message": f"{identity} {path} 값이 기대값과 다릅니다.",
                })
    for identity, count in (actual_counts - expected_counts).items():
        issues.append({"category": category, "mapping_id": "", "message": f"예상하지 않은 리소스 {identity}: {count}건"})
    return {
        "status": "evaluated",
        "source_field_accuracy": _score(correct, evaluated),
        "resource_count_alignment": _detection(expected_counts, actual_counts),
        "issues": issues,
    }


DEFAULT_MATCH_PATHS = {
    "Patient": ("id",),
    "Encounter": ("id",),
    "Condition": (
        "subject.reference", "encounter.reference", "code.coding.0.code", "onsetDateTime",
    ),
    "Observation": (
        "subject.reference", "encounter.reference", "code.coding.0.code", "effectiveDateTime",
    ),
}


def project_comparison_resources(
    resources: list[dict[str, Any]],
    *,
    date_only_paths: dict[str, list[str]] | None = None,
    component_events: dict[str, dict[str, Any]] | None = None,
    copy_paths: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """공통 원천·참조 해상도에 맞는 평가 전용 사본을 만듭니다.

    구성요소 규칙이 적용되는 패널은 패널 자체를 집계하지 않고 구성요소마다
    하나의 이벤트로 투영합니다. 원천에 구성요소만 기록되는 경우에만 적용해야
    합니다. 표현이 다른 동등한 값은 별칭 경로를 통해 비교할 수 있습니다.
    투영 결과는 표준 적합성 검증이나 출력 리소스에 쓰지 않습니다.
    """
    projected: list[dict[str, Any]] = []
    for resource in resources:
        kind = resource.get("resourceType")
        rule = (component_events or {}).get(kind)
        components = get_path(resource, rule["path"]) if rule else None
        if isinstance(components, list) and components and all(isinstance(item, dict) for item in components):
            for component in components:
                event = {**deepcopy(component), "resourceType": kind}
                for path in rule["inherit"]:
                    value = get_path(resource, path)
                    if value is not None:
                        set_path(event, path, deepcopy(value))
                projected.append(event)
        else:
            projected.append(deepcopy(resource))

    for event in projected:
        kind = event.get("resourceType")
        for target, source in (copy_paths or {}).get(kind, {}).items():
            value = get_path(event, source)
            if value is not None and get_path(event, target) is None:
                set_path(event, target, deepcopy(value))
        for path in (date_only_paths or {}).get(kind, []):
            value = get_path(event, path)
            if isinstance(value, str):
                try:
                    date = datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
                except ValueError:
                    continue
                set_path(event, path, date)
    return projected


def evaluate_reference_resources(
    generated: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    *,
    match_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
    field_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """독립 참조 FHIR를 사전 고정한 이벤트 정렬·평가 범위로 비교합니다.

    Condition/Observation의 exporter 고유 ID는 이벤트 식별과 필드 점수에서 제외합니다.
    반복 이벤트는 중복 수를 보존하며, 비교 범위는 실험 설정으로 고정해야 합니다.
    date와 full datetime은 같게 취급하지 않습니다. CSV에 날짜만 존재한다면
    공통 시간 해상도에 맞는 match_paths/field_paths를 실험 전에 명시해야 합니다.
    전체 필드 점수는 누락 이벤트를 포함하고, 대응된 이벤트의 필드 점수는 따로 냅니다.
    기대 Reference는 범위 내 reference 경로를 기준으로 집계합니다. 매칭에
    필요한 Reference 자체가 다르면 이벤트 미매칭으로 분류됩니다.
    """
    paths = {**DEFAULT_MATCH_PATHS, **(match_paths or {})}

    def event_key(resource: dict[str, Any]) -> str:
        kind = str(resource.get("resourceType", ""))
        values = [_canonical_value(get_path(resource, path), path) for path in paths.get(kind, ("id",))]
        return json.dumps([kind, *values], ensure_ascii=False, sort_keys=True)

    actual_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    expected_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in generated:
        actual_groups[event_key(resource)].append(resource)
    for resource in expected:
        expected_groups[event_key(resource)].append(resource)

    def scoped_fields(resource: dict[str, Any]) -> dict[str, Any]:
        kind = str(resource.get("resourceType", ""))
        selected = field_paths.get(kind) if field_paths is not None else None
        result: dict[str, Any] = {}
        for path, value in _leaves(resource).items():
            root = path.split(".", 1)[0]
            if root in {"resourceType", "meta", "text"} or (root == "id" and kind in {"Condition", "Observation"}):
                continue
            if selected is not None and not any(path == item or path.startswith(f"{item}.") for item in selected):
                continue
            result[path] = value
        return result

    # Duplicate events remain a multiset; input ordering must not affect the score.
    for grouping in (actual_groups, expected_groups):
        for items in grouping.values():
            items.sort(key=lambda item: json.dumps(
                {path: _canonical_value(value, path) for path, value in scoped_fields(item).items()},
                ensure_ascii=False, sort_keys=True, default=str,
            ))
    detection = _detection(
        Counter({key: len(value) for key, value in expected_groups.items()}),
        Counter({key: len(value) for key, value in actual_groups.items()}),
    )
    issues: list[dict[str, Any]] = []
    correct = evaluated = generated_fields = 0
    matched_event_fields = matched_event_correct = 0
    expected_references = present_references = correct_references = 0
    extra_fields = 0
    missing = object()
    for key, expected_items in expected_groups.items():
        actual_items = actual_groups.get(key, [])
        for index, resource in enumerate(expected_items):
            matched = index < len(actual_items)
            actual_fields = scoped_fields(actual_items[index]) if matched else {}
            if not matched:
                issues.append({"category": "independent_fidelity", "mapping_id": "", "message": f"기대 이벤트 누락: {key}"})
            expected_fields = scoped_fields(resource)
            generated_fields += len(actual_fields)
            if matched:
                matched_event_fields += len(expected_fields)
            for path, value in expected_fields.items():
                evaluated += 1
                actual_value = actual_fields.get(path, missing)
                equal = _equivalent(actual_value, value, path)
                if equal:
                    correct += 1
                    matched_event_correct += int(matched)
                else:
                    issues.append({"category": "independent_fidelity", "mapping_id": "", "path": path, "message": f"{key} {path} 값이 독립 참조와 다릅니다."})
                if path.endswith(".reference") and isinstance(value, str) and value.strip():
                    expected_references += 1
                    if isinstance(actual_value, str) and actual_value.strip():
                        present_references += 1
                        correct_references += int(equal)
            for path in actual_fields.keys() - expected_fields.keys():
                extra_fields += 1
                issues.append({
                    "category": "independent_fidelity", "mapping_id": "", "path": path,
                    "message": f"{key} 독립 참조에 없는 평가 범위 내 필드가 생성되었습니다: {path}",
                })
    for key, actual_items in actual_groups.items():
        extra = len(actual_items) - len(expected_groups.get(key, []))
        if extra > 0:
            issues.append({"category": "independent_fidelity", "mapping_id": "", "message": f"예상하지 않은 이벤트 {key}: {extra}건"})
            for resource in actual_items[len(expected_groups.get(key, [])):]:
                count = len(scoped_fields(resource))
                generated_fields += count
                extra_fields += count
    field_precision = correct / generated_fields if generated_fields else (1.0 if not evaluated else 0.0)
    field_recall = correct / evaluated if evaluated else (1.0 if not generated_fields else 0.0)
    field_f1 = 2 * field_precision * field_recall / (field_precision + field_recall) if field_precision + field_recall else 0.0
    return {
        "status": "evaluated",
        "valid": not issues,
        "pass": not issues,
        "source_field_accuracy": _score(correct, evaluated),
        "event_match_coverage": _score(detection["matched"], detection["expected"]),
        "matched_event_field_accuracy": _score(matched_event_correct, matched_event_fields),
        "expected_reference_metrics": _expected_reference_metrics(
            expected_references, present_references, correct_references,
        ),
        "field_detection": {
            "expected": evaluated, "generated": generated_fields, "matched": correct,
            "extra_fields": extra_fields,
            "false_positive": generated_fields - correct, "false_negative": evaluated - correct,
            "precision": field_precision, "recall": field_recall, "f1": field_f1,
        },
        "resource_count_alignment": detection,
        "event_detection": dict(detection),
        "issues": issues,
        "match_paths": {key: list(value) for key, value in paths.items()},
        "field_paths": field_paths,
    }


def evaluate_fidelity(
    profile: dict[str, Any],
    records: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    timezone: str | None = None,
    *,
    expected_resources: list[dict[str, Any]] | None = None,
    reference_resources: list[dict[str, Any]] | None = None,
    match_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
    field_paths: dict[str, list[str] | tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """독립 정답만으로 평가합니다. profile과 records로 정답을 생성하지 않습니다.

    ID는 사전에 검토한 이벤트 정렬 규칙으로 정규화되어 있어야 합니다.
    """
    if expected_resources is None:
        result: dict[str, Any] = {
            "status": "not_evaluated",
            "reason": "독립 expected_resources가 제공되지 않았습니다.",
            "source_field_accuracy": _score(0, 0),
            "event_match_coverage": _score(0, 0),
            "matched_event_field_accuracy": _score(0, 0),
            "expected_reference_metrics": _expected_reference_metrics(0, 0, 0),
            "resource_count_alignment": {"status": "not_evaluated", "alignment_rate": None},
            "issues": [],
        }
    else:
        result = evaluate_reference_resources(resources, expected_resources, match_paths=match_paths, field_paths=field_paths)
    references = evaluate_references(resources, reference_resources)
    result["reference_accuracy"] = {key: value for key, value in references.items() if key != "issues"}
    result["issues"].extend(references["issues"])
    return result


def evaluate_profile_execution_consistency(
    profile: dict[str, Any],
    records: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    timezone: str | None = None,
    *,
    source_indices: list[int] | None = None,
) -> dict[str, Any]:
    """Profile 실행 재현성입니다. 매핑 정답 정확도로 사용하면 안 됩니다."""
    expected_resources: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    indices = range(len(records)) if source_indices is None else source_indices
    for index in indices:
        record = records[index]
        try:
            expected: dict[str, Any] = {"resourceType": profile["target_resource"], "id": resource_id(profile, record)}
            for mapping in profile["mappings"]:
                value = apply_mapping(mapping, record, timezone)
                if value is not OMIT:
                    set_path(expected, mapping["target_path"], value)
            expected_resources.append(prune(expected))
        except (ValueError, TypeError) as exc:
            errors.append({"category": "profile_execution", "mapping_id": "", "message": f"record[{index}]: {exc}"})
    result = _compare_resources(expected_resources, resources, category="profile_execution")
    result["issues"] = [*errors, *result["issues"]]
    result["measurement"] = "profile_execution_consistency"
    return result


def evaluate_source_preservation(
    records: list[dict[str, Any]], resources: list[dict[str, Any]],
    checks: list[dict[str, Any]], *, source_indices: list[int],
) -> dict[str, Any]:
    """사전 승인된 원천 보존 assertion을 실행합니다. 생성 Profile은 사용하지 않습니다."""
    issues = []
    correct = evaluated = 0
    for resource, index in zip(resources, source_indices, strict=True):
        for check in checks:
            expected = apply_mapping(check, records[index], None)
            actual = get_path(resource, check["target_path"])
            evaluated += 1
            matches = actual is None if expected is OMIT else _equivalent(actual, expected, check["target_path"])
            if matches:
                correct += 1
            else:
                issues.append({
                    "category": "source_preservation", "mapping_id": "",
                    "path": check["target_path"], "check_id": check["id"],
                    "expected_value": None if expected is OMIT else expected, "actual_value": actual,
                    "expected_missing": expected is OMIT,
                    "message": f"record[{index}] {check['target_path']} 원천 보존 검사 {check['id']} 실패",
                })
    return {**_score(correct, evaluated), "issues": issues}
