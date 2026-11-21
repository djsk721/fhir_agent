from __future__ import annotations

from copy import deepcopy
from typing import Any


def revise_profile(
    profile: dict[str, Any],
    issues: list[dict[str, Any]],
    records: list[dict[str, Any]] | None = None,
    *,
    unit_aliases: dict[str, str] | None = None,
    structural_defaults: dict[str, Any] | None = None,
    assumed_timezone: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """명시적으로 승인한 보조 규칙만 적용합니다.

    기본 호출은 status나 timezone, UCUM 의미를 추측하지 않습니다. 연구 주경로는
    Mapping Agent의 profile_reviser를 사용하며 이 함수는 선택적 보조 정책입니다.
    """
    revised = deepcopy(profile)
    changes: list[dict[str, str]] = []
    mappings = revised["mappings"]
    roots = {mapping["target_path"].split(".", 1)[0] for mapping in mappings}
    for field_name, value in (structural_defaults or {}).items():
        missing = any(field_name in issue.get("message", "") for issue in issues)
        if missing and field_name not in roots:
            mapping_id = f"approved-default-{field_name}"
            mappings.append({"id": mapping_id, "target_path": field_name, "operation": "constant", "value": value})
            changes.append({"mapping_id": mapping_id, "change": f"apply explicitly approved default {field_name}={value}"})
    if assumed_timezone is not None:
        for issue in issues:
            if issue.get("category") != "datetime_timezone":
                continue
            mapping = next((item for item in mappings if item.get("id") == issue.get("mapping_id")), None)
            if mapping and mapping.get("timezone") != assumed_timezone:
                mapping["timezone"] = assumed_timezone
                changes.append({"mapping_id": mapping["id"], "change": "apply explicitly configured source timezone"})
    if unit_aliases and any("UCUM" in str(issue.get("message", "")) or "unitsofmeasure" in str(issue.get("message", "")) for issue in issues):
        for mapping in mappings:
            matching = any(issue.get("mapping_id") == mapping.get("id") for issue in issues)
            if matching and mapping.get("target_path") == "valueQuantity.code" and mapping.get("operation") in {"copy", "string"}:
                mapping.update(operation="lookup", table=dict(unit_aliases), fallback="source")
                changes.append({"mapping_id": mapping["id"], "change": "apply explicitly configured UCUM lookup"})
    return revised, changes
