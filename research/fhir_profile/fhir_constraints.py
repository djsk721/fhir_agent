"""Exact read-only FHIR constraints from a local, versioned package.

This is complementary to text retrieval. It preserves structural facts from
StructureDefinition snapshots without relying on similarity scores or an LLM.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


_BASE_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CARDINALITY = re.compile(r"(?:\*|[0-9]+)")


def _json_object(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"유효하지 않은 FHIR 패키지 JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"FHIR 패키지 JSON 객체가 필요합니다: {path.name}")
    return value


def _choice_variant(path: str, code: str) -> str:
    # Preserve the concrete JSON path, without changing the group cardinality.
    return f"{path.removesuffix('[x]')}{code[:1].upper()}{code[1:]}"


def _element_facts(element: dict[str, Any], canonical: str) -> dict[str, Any]:
    path = element.get("path")
    lower, upper = element.get("min"), element.get("max")
    if (not isinstance(path, str) or not isinstance(lower, int) or isinstance(lower, bool) or lower < 0
            or not isinstance(upper, str) or _CARDINALITY.fullmatch(upper) is None
            or (upper != "*" and lower > int(upper))):
        raise ValueError(f"FHIR snapshot 요소의 path/cardinality가 잘못되었습니다: {path}")
    facts: dict[str, Any] = {"path": path, "min": lower, "max": upper}
    element_id = element.get("id")
    if element_id and element_id != path:
        facts["id"] = element_id
    types = []
    variants = element.get("type", [])
    if not isinstance(variants, list) or any(not isinstance(variant, dict) for variant in variants):
        raise ValueError(f"FHIR snapshot 요소의 type 배열이 잘못되었습니다: {path}")
    for variant in variants:
        code = variant.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError(f"FHIR snapshot 요소의 type.code가 필요합니다: {path}")
        data_type = {key: variant[key] for key in ("code", "profile", "targetProfile", "aggregation", "versioning")
                     if key in variant}
        if path.endswith("[x]") and _BASE_TYPE.fullmatch(code):
            data_type["json_path"] = _choice_variant(path, code)
        types.append(data_type)
    if types:
        facts["types"] = types
    for key in ("contentReference", "sliceName", "slicing", "isModifier", "mustSupport"):
        if key in element and element[key]:
            facts[key] = element[key]
    binding = element.get("binding")
    if binding:
        facts["binding"] = {key: binding[key] for key in ("strength", "valueSet") if key in binding}
    # The validator still executes inherited invariants; avoid copying them into
    # every prompt element and obscuring the resource's own constraints.
    constraints = [
        {key: rule[key] for key in ("key", "severity", "human", "expression") if key in rule}
        for rule in element.get("constraint", [])
        if rule.get("source", canonical) == canonical
    ]
    if constraints:
        facts["constraints"] = constraints
    return facts


class FhirConstraintLookup:
    """Load core R4 snapshot facts and give the experiment a content identity."""

    def __init__(self, package_path: str | Path, *, fhir_version: str = "4.0.1") -> None:
        path = Path(package_path).expanduser()
        if not (path / "package.json").is_file() and (path / "package" / "package.json").is_file():
            path /= "package"
        if not (path / "package.json").is_file():
            raise ValueError("FHIR 패키지에서 package.json을 찾을 수 없습니다.")
        manifest = path / "package.json"
        raw = manifest.read_bytes()
        metadata = _json_object(raw, manifest)
        versions = metadata.get("fhirVersions")
        if not isinstance(versions, list) or fhir_version not in versions:
            raise ValueError(f"FHIR 패키지에 요청한 FHIR 버전이 없습니다: {fhir_version}")
        if not all(isinstance(metadata.get(field), str) and metadata[field] for field in ("name", "version")):
            raise ValueError("FHIR 패키지의 이름과 버전이 필요합니다.")
        self._path = path.resolve()
        self._files: dict[str, str] = {
            candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()
            for candidate in sorted(self._path.glob("StructureDefinition-*.json")) if candidate.is_file()
        }
        if not self._files:
            raise ValueError("FHIR 패키지에 StructureDefinition 파일이 없습니다.")
        manifest_entry = {"package.json": hashlib.sha256(raw).hexdigest()}
        content = json.dumps({**manifest_entry, **self._files}, sort_keys=True, separators=(",", ":"))
        self._identity = {
            "kind": "fhir_structuredef_snapshot_v1",
            "package": metadata["name"],
            "version": metadata["version"],
            "fhir_version": fhir_version,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    @property
    def identity(self) -> dict[str, str]:
        return dict(self._identity)

    def constraints(self, resource_type: str) -> dict[str, Any]:
        if not isinstance(resource_type, str) or _BASE_TYPE.fullmatch(resource_type) is None:
            raise ValueError("FHIR resourceType 형식이 올바르지 않습니다.")
        name = f"StructureDefinition-{resource_type}.json"
        if name not in self._files:
            raise ValueError(f"FHIR 패키지에 대상 StructureDefinition이 없습니다: {resource_type}")
        path = self._path / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self._files[name]:
            raise ValueError(f"FHIR StructureDefinition이 조회 중 변경되었습니다: {resource_type}")
        snapshot = _json_object(raw, path)
        canonical = snapshot.get("url")
        if (snapshot.get("resourceType") != "StructureDefinition" or snapshot.get("type") != resource_type
                or snapshot.get("kind") != "resource" or snapshot.get("fhirVersion") != self._identity["fhir_version"]
                or not isinstance(canonical, str) or not canonical):
            raise ValueError(f"대상 리소스/버전과 일치하는 FHIR StructureDefinition이 아닙니다: {resource_type}")
        payload = snapshot.get("snapshot")
        elements = payload.get("element") if isinstance(payload, dict) else None
        if (not isinstance(elements, list) or not elements
                or any(not isinstance(element, dict) for element in elements)
                or elements[0].get("path") != resource_type):
            raise ValueError(f"완전한 FHIR StructureDefinition snapshot이 필요합니다: {resource_type}")
        return {
            "source": "FHIR StructureDefinition snapshot",
            "resource_type": resource_type,
            "canonical": canonical,
            "fhir_version": self._identity["fhir_version"],
            # Preserve disabled elements (max=0) because they are prohibitions.
            "elements": [_element_facts(element, canonical) for element in elements],
        }

    def context(self, resource_type: str) -> str:
        return json.dumps(self.constraints(resource_type), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
