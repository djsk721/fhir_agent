"""평가 단계에서만 사용하는 독립 FHIR 참조 로더."""
from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any

from .renderer.operations import resource_id


def load_reference_resources(location: str | Path, *, patient_keys: list[Any], resource_types: list[str],
                             identity_rules: dict[str, dict[str, str]] | None = None,
                             file_patterns: list[str] | None = None) -> list[dict[str, Any]]:
    if not patient_keys or len({str(key) for key in patient_keys}) != len(patient_keys):
        raise ValueError("독립 참조를 불러올 환자 키는 비어 있지 않고 중복이 없어야 합니다.")
    if not resource_types or "Patient" not in resource_types:
        raise ValueError("독립 참조 범위에는 Patient와 평가할 resourceType이 필요합니다.")
    path = Path(location)
    if not path.exists():
        raise FileNotFoundError(path)
    if file_patterns and any(Path(pattern).is_absolute() or ".." in Path(pattern).parts for pattern in file_patterns):
        raise ValueError("참조 파일 패턴은 입력 디렉터리 내부의 상대 경로여야 합니다.")
    files = [path]
    if path.is_dir():
        files = sorted({p for pattern in (file_patterns or ["**/*"]) for p in path.glob(pattern)
                        if p.is_file() and p.name.endswith((".json", ".ndjson", ".ndjson.gz"))})
        if not files or (file_patterns and any(not list(path.glob(pattern)) for pattern in file_patterns)):
            raise ValueError("참조 파일 패턴에 해당하는 파일이 없습니다.")
    entries: list[dict[str, Any]] = []
    for file in files:
        opener = gzip.open if file.suffix == ".gz" else open
        with opener(file, "rt", encoding="utf-8-sig") as handle:
            payloads = [json.loads(line) for line in handle if line.strip()] if ".ndjson" in file.name else [json.load(handle)]
        for payload in payloads:
            for obj in payload if isinstance(payload, list) else [payload]:
                if not isinstance(obj, dict):
                    raise ValueError("독립 참조는 FHIR resource/Bundle 객체여야 합니다.")
                if obj.get("resourceType") == "Bundle":
                    entries.extend(deepcopy(obj.get("entry", [])))
                elif obj.get("resourceType"):
                    entries.append({"resource": deepcopy(obj)})
                else:
                    raise ValueError(f"FHIR 참조 형식이 아닙니다: {file.name}")
    aliases: dict[str, str] = {}
    def add_alias(alias: str, target: str) -> None:
        if alias in aliases and aliases[alias] != target:
            raise ValueError(f"독립 참조에 충돌하는 alias가 있습니다: {alias}")
        aliases[alias] = target

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            raise ValueError("FHIR Bundle entry에 resource 객체가 필요합니다.")
        resource = entry["resource"]
        kind = resource.get("resourceType")
        old_id = resource.get("id")
        rule = (identity_rules or {}).get(kind)
        if rule:
            system = rule.get("identifier_system")
            if not system:
                raise ValueError("identity rule에 identifier_system이 필요합니다.")
            values = {str(item["value"]) for item in resource.get("identifier", [])
                      if item.get("system") == system and item.get("value") not in (None, "")}
            if len(values) != 1:
                raise ValueError(f"{kind}/{old_id}의 식별자를 하나로 결정할 수 없습니다: {system}")
            resource["id"] = resource_id({"identity": {"source_fields": ["key"], "prefix": rule.get("prefix", "")}}, {"key": values.pop()})
        target = f"{kind}/{resource.get('id')}"
        if old_id:
            add_alias(f"{kind}/{old_id}", target)
        full_url = entry.get("fullUrl")
        if full_url is not None:
            if not isinstance(full_url, str) or not full_url or not resource.get("id") or not resource.get("resourceType"):
                raise ValueError("FHIR fullUrl에는 resourceType/id가 있는 리소스가 필요합니다.")
            add_alias(full_url, target)

    def normalize(value):
        if isinstance(value, dict):
            if isinstance(value.get("reference"), str) and value["reference"] in aliases:
                value["reference"] = aliases[value["reference"]]
            for nested in value.values():
                normalize(nested)
        elif isinstance(value, list):
            for nested in value:
                normalize(nested)

    keys = {str(key) for key in patient_keys}
    selected = {}
    for entry in entries:
        resource = entry.get("resource", {})
        normalize(resource)
        kind = resource.get("resourceType")
        if kind not in resource_types:
            continue
        if not isinstance(resource.get("id"), str) or not resource["id"]:
            raise ValueError(f"독립 참조 {kind}에 이벤트 정렬에 필요한 id가 없습니다.")
        if kind == "Patient":
            include = str(resource.get("id")) in keys
        else:
            reference = resource.get("subject", {}).get("reference", "")
            include = reference.startswith("Patient/") and reference[len("Patient/"):] in keys
        if include:
            identity = (kind, resource.get("id"))
            if identity in selected and selected[identity] != resource:
                raise ValueError(f"독립 참조에 충돌하는 resource ID가 있습니다: {identity}")
            selected[identity] = resource
    actual_keys = {str(resource["id"]) for resource in selected.values() if resource.get("resourceType") == "Patient"}
    if actual_keys != keys:
        missing = sorted(keys - actual_keys)
        raise ValueError(f"전체 평가 환자의 참조 Patient가 필요합니다. 누락: {missing}. 원천 키와 FHIR identifier/id 정렬 규칙을 확인하세요.")
    return list(selected.values())
