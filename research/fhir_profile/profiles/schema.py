from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any


SUPPORTED_RESOURCES = ("Patient", "Encounter", "Condition", "Observation")
OPERATIONS = {
    "string", "integer", "decimal", "boolean", "date", "datetime", "constant",
    "lookup", "reference", "coding", "codeable_concept", "identifier", "copy",
}
NULL_POLICIES = {"omit", "null", "error"}
FORBIDDEN_KEYS = {"sql", "query", "statement", "eval", "exec", "python", "expression"}
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db/fhir.schema.json"


@lru_cache(maxsize=4)
def _schema(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_profile(
    path: str | Path,
    metadata: dict[str, Any] | None = None,
    *,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    return validate_profile(
        json.loads(Path(path).read_text(encoding="utf-8")), metadata,
        schema_path=schema_path,
    )


def _resolve(node: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    visited: set[str] = set()
    while "$ref" in node:
        reference = node["$ref"]
        if not reference.startswith("#/definitions/") or reference in visited:
            raise ValueError(f"지원하지 않는 FHIR schema reference입니다: {reference}")
        visited.add(reference)
        node = schema["definitions"][reference.rsplit("/", 1)[-1]]
    return node


def _parts(path: str) -> list[str | int]:
    if not path or any(not segment for segment in path.split(".")):
        raise ValueError(f"잘못된 FHIR target_path입니다: {path!r}")
    return [int(segment) if segment.isdigit() else segment for segment in path.split(".")]


def _choice_groups(node: dict[str, Any], schema: dict[str, Any]) -> dict[str, str]:
    """FHIR의 type suffix를 schema로부터 읽어 choice sibling을 찾습니다."""
    suffixes = sorted(
        {name[0].upper() + name[1:] for name in schema["definitions"] if "_" not in name},
        key=len, reverse=True,
    )
    candidates: dict[str, list[str]] = {}
    for name in node.get("properties", {}):
        if name.startswith("_"):
            continue
        for suffix in suffixes:
            if name.endswith(suffix) and len(name) > len(suffix):
                prefix = name[:-len(suffix)]
                candidates.setdefault(prefix, []).append(name)
                break
    return {
        member: prefix for prefix, members in candidates.items() if len(members) > 1
        for member in members
    }


def _target_info(
    resource: str, path: str, schema: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    node = schema["definitions"][resource]
    traversed: list[str] = []
    choices: list[tuple[str, str]] = []
    for part in _parts(path):
        node = _resolve(node, schema)
        if isinstance(part, int):
            if node.get("type") != "array":
                raise ValueError(f"배열이 아닌 FHIR 요소에 index가 있습니다: {path}")
            node = node["items"]
        else:
            properties = node.get("properties", {})
            if part not in properties:
                raise ValueError(f"FHIR schema에 없는 target_path입니다: {resource}.{path}")
            group = _choice_groups(node, schema).get(part)
            if group:
                choices.append((".".join([*traversed, group]), part))
            node = properties[part]
        traversed.append(str(part))
    return _resolve(node, schema), choices


def _target_operations(target: dict[str, Any], schema: dict[str, Any]) -> frozenset[str]:
    """Operations by FHIR output datatype; never infer source-field mappings here."""
    definitions = schema["definitions"]
    def primitive_is(*names: str) -> bool:
        for name in names:
            primitive = definitions.get(name, {})
            # Choice elements may inline the primitive's constraints rather
            # than using a reference to its type.
            inline = target.get("pattern") and (target.get("type"), target["pattern"]) == (
                primitive.get("type"), primitive.get("pattern"))
            if target is primitive or inline:
                return True
        return False

    allowed = {"constant", "copy", "lookup"}
    for datatype, operation in (
        ("Reference", "reference"), ("Coding", "coding"), ("CodeableConcept", "codeable_concept"),
    ):
        if target is definitions.get(datatype):
            allowed.add(operation)
    datatype = target.get("type")
    if datatype is None and target.get("enum") and all(isinstance(value, str) for value in target["enum"]):
        datatype = "string"
    if datatype == "array" and _resolve(target.get("items", {}), schema) is definitions.get("Identifier"):
        allowed.add("identifier")
    elif datatype == "string":
        allowed.add("string")
        if primitive_is("date", "dateTime"):
            allowed.add("date")
        if primitive_is("dateTime", "instant"):
            allowed.add("datetime")
    elif datatype == "boolean":
        allowed.add("boolean")
    elif datatype in ("integer", "number"):
        allowed.add("integer")
        if datatype == "number" and not primitive_is("integer", "positiveInt", "unsignedInt"):
            allowed.add("decimal")
    return frozenset(allowed)


def _columns(metadata: dict[str, Any] | None) -> set[str] | None:
    if metadata is None or "columns" not in metadata:
        return None
    columns = metadata["columns"]
    if isinstance(columns, dict):
        return set(columns)
    if not isinstance(columns, list):
        raise ValueError("metadata.columns는 컬럼 배열 또는 객체여야 합니다.")
    names: set[str] = set()
    for column in columns:
        name = column if isinstance(column, str) else column.get("name") if isinstance(column, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("metadata.columns의 컬럼에는 name이 필요합니다.")
        names.add(name)
    return names


def _source_field(value: Any, columns: set[str] | None, location: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location}에는 원천 필드명이 필요합니다.")
    if columns is not None and value not in columns:
        raise ValueError(f"{location}의 unknown source field: {value}")


def _identity(identity: Any, columns: set[str] | None, location: str) -> None:
    fields = identity.get("source_fields") if isinstance(identity, dict) else None
    if not isinstance(fields, list) or not fields:
        raise ValueError(f"{location}.source_fields에는 원천 필드 배열이 필요합니다.")
    for field in fields:
        _source_field(field, columns, f"{location}.source_fields")
    if len(set(fields)) != len(fields):
        raise ValueError(f"{location}.source_fields에 중복이 있습니다.")
    for option in ("separator", "prefix"):
        if option in identity and not isinstance(identity[option], str):
            raise ValueError(f"{location}.{option}는 문자열이어야 합니다.")


def _identity_encoding(identity: dict[str, Any]) -> tuple[str, str]:
    return (identity.get("prefix", ""), identity.get("separator", "-"))


def canonical_identity(metadata: dict[str, Any] | None) -> dict[str, list[str]] | None:
    """Use a reviewed business key first, or a trusted extractor identity in managed runs."""
    metadata = metadata or {}
    fields = metadata.get("identity_fields")
    columns = _columns(metadata)
    if not fields and metadata.get("identity_contract") is True:
        fields = metadata.get("record_identity_fields")
        # Extractor-only locator fields are deliberately outside the clinical mapping scope.
        columns = None
        if not fields:
            raise ValueError("관리형 identity에는 검토한 원천 업무키 또는 추출기의 레코드 키가 필요합니다.")
    if not fields:
        return None
    identity = {"source_fields": deepcopy(fields)}
    _identity(identity, columns, "원천 업무키 계약")
    return identity


def bind_profile_identity(profile: dict[str, Any], metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Install the source-owned ID instead of trusting the model's identity proposal."""
    if (metadata or {}).get("identity_contract") is not True:
        return profile
    if not isinstance(profile, dict):
        raise ValueError("Conversion Profile은 JSON 객체여야 합니다.")
    return {**profile, "identity": canonical_identity(metadata)}


def _validate_reference_identity(
    mapping: dict[str, Any], metadata: dict[str, Any], location: str,
) -> bool | None:
    """Check target encoding and report whether a matched source FK is nullable."""
    target = metadata.get("related_sources", {}).get(mapping["resource_type"], {})
    target_identity = target.get("identity")
    if target_identity is None and target.get("identity_fields"):
        target_identity = {"source_fields": target["identity_fields"]}
    if not target_identity:
        nullability = metadata.get("column_nullable") or {}
        local = mapping.get("identity", {}).get("source_fields") or [mapping["source_field"]]
        return _columns_nullable(nullability, local)
    fields = target_identity["source_fields"]
    acceptable = []
    for foreign_key in metadata.get("foreign_keys", []):
        if foreign_key.get("referred_table") != target.get("table"):
            continue
        if target.get("schema") and foreign_key.get("referred_schema") not in (None, target["schema"]):
            continue
        local_by_target = dict(zip(foreign_key["referred_columns"], foreign_key["columns"], strict=True))
        if all(field in local_by_target for field in fields):
            source_fields = [local_by_target[field] for field in fields]
            nullable = foreign_key.get("nullable")
            if nullable is None:
                nullable = _columns_nullable(metadata.get("column_nullable") or {}, source_fields)
            if nullable is not None and type(nullable) is not bool:
                raise ValueError("foreign_keys.nullable은 boolean이어야 합니다.")
            acceptable.append((source_fields, nullable))
    if not acceptable:
        if metadata.get("identity_contract") is True:
            raise ValueError(f"{location}의 {mapping['resource_type']} 연결을 뒷받침할 원천 외래 키 선언이 없습니다.")
        return None
    actual = (mapping["identity"] if "identity" in mapping else
              {"source_fields": [mapping["source_field"]]})
    matching = [nullable for source_fields, nullable in acceptable if source_fields == actual["source_fields"]]
    if not matching or _identity_encoding(actual) != _identity_encoding(target_identity):
        expected_fields = [source_fields for source_fields, _ in acceptable]
        raise ValueError(f"{location}는 {mapping['resource_type']} 원천 외래 키 {expected_fields}와 동일한 ID 인코딩을 사용해야 합니다.")
    if False in matching:
        return False
    return True if all(value is True for value in matching) else None


def _columns_nullable(nullability: dict[str, bool], fields: list[str]) -> bool | None:
    values = [nullability.get(name) for name in fields]
    if any(value is False for value in values):
        return False
    return True if all(value is True for value in values) else None


def _reference_path_required(resource: str, path: str, schema: dict[str, Any]) -> bool:
    """Read an already-validated Reference path's local cardinality from JSON Schema."""
    node = schema["definitions"][resource]
    required = False
    for part in _parts(path):
        node = _resolve(node, schema)
        if isinstance(part, int):
            required = part < node.get("minItems", 0)
            node = node["items"]
        else:
            required = part in node.get("required", ())
            node = node["properties"][part]
    return required


def _condition(condition: Any, columns: set[str] | None, location: str) -> None:
    if not isinstance(condition, dict) or not condition:
        raise ValueError(f"{location}는 선언적 조건 객체여야 합니다.")
    for operator in ("all", "any", "not"):
        if operator in condition:
            if set(condition) != {operator}:
                raise ValueError(f"{location}의 복합 조건은 {operator}만 포함해야 합니다.")
            children = condition[operator] if operator != "not" else [condition[operator]]
            if not isinstance(children, list) or not children:
                raise ValueError(f"{location}.{operator}에는 조건 배열이 필요합니다.")
            for child in children:
                _condition(child, columns, location)
            return
    operators = set(condition) - {"field"}
    if len(operators) != 1 or not operators <= {"equals", "in", "present"}:
        raise ValueError(f"{location}는 field와 equals/in/present 하나만 허용합니다.")
    _source_field(condition.get("field"), columns, f"{location}.field")
    if "in" in condition and (not isinstance(condition["in"], list) or not condition["in"]):
        raise ValueError(f"{location}.in에는 값 배열이 필요합니다.")
    if "present" in condition and not isinstance(condition["present"], bool):
        raise ValueError(f"{location}.present는 boolean이어야 합니다.")


def conditions_disjoint(first: Any, second: Any) -> bool:
    """안전하게 증명할 수 있는 경우에만 두 조건의 상호 배타성을 인정합니다."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if "all" in first:
        return any(conditions_disjoint(part, second) for part in first["all"])
    if "all" in second:
        return any(conditions_disjoint(first, part) for part in second["all"])
    if "any" in first:
        return all(conditions_disjoint(part, second) for part in first["any"])
    if "any" in second:
        return all(conditions_disjoint(first, part) for part in second["any"])
    if first.get("not") == second or second.get("not") == first:
        return True
    if first.get("field") is None or first.get("field") != second.get("field"):
        return False
    if "present" in first and "present" in second:
        return first["present"] != second["present"]
    values_a = [first["equals"]] if "equals" in first else first.get("in")
    values_b = [second["equals"]] if "equals" in second else second.get("in")
    if values_a is not None and values_b is not None:
        return not any(a == b for a in values_a for b in values_b)
    return False


def validate_profile(
    profile: dict[str, Any], metadata: dict[str, Any] | None = None,
    *, schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """FHIR 전체 경로·원천 필드·선언적 연산·choice 충돌을 사전 검증합니다."""
    if not isinstance(profile, dict):
        raise ValueError("Conversion Profile은 JSON 객체여야 합니다.")
    _reject_forbidden_keys(profile)
    required = ("profile_id", "profile_version", "target_resource", "fhir_version", "source", "identity", "mappings")
    missing = [key for key in required if key not in profile]
    if missing:
        raise ValueError(f"Conversion Profile 필수 항목 누락: {missing}")
    resource = profile["target_resource"]
    if resource not in SUPPORTED_RESOURCES:
        raise ValueError(f"지원하지 않는 target_resource입니다: {resource!r}")
    if str(profile["fhir_version"]) not in {"R4", "4.0.1"}:
        raise ValueError("fhir_version은 R4 또는 4.0.1이어야 합니다.")
    source = profile["source"]
    if not isinstance(source, dict) or not source.get("dataset") or not source.get("table"):
        raise ValueError("source.dataset과 source.table이 필요합니다.")
    columns = _columns(metadata)
    canonical = canonical_identity(metadata)
    managed = (metadata or {}).get("identity_contract") is True
    identity_columns = columns
    if managed and canonical is not None and columns is not None:
        identity_columns = columns | set(canonical["source_fields"])
    _identity(profile["identity"], identity_columns, "identity")
    if canonical:
        if (profile["identity"]["source_fields"] != canonical["source_fields"]
                or _identity_encoding(profile["identity"]) != _identity_encoding(canonical)):
            raise ValueError(f"identity는 명세에 선언된 원천 업무키 계약 {canonical['source_fields']}를 기본 ID 인코딩으로 사용해야 합니다.")
    mappings = profile["mappings"]
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("mappings에는 하나 이상의 변환 규칙이 필요합니다.")
    schema = _schema(str(Path(schema_path or DEFAULT_SCHEMA_PATH).resolve()))
    seen: set[str] = set()
    targets: list[tuple[str, list[tuple[str, str]], dict[str, Any]]] = []
    reference_null_policies: dict[int, str] = {}
    for index, mapping in enumerate(mappings):
        location = f"mappings[{index}]"
        _validate_mapping(mapping, columns, seen, location)
        path = mapping["target_path"]
        if path.split(".", 1)[0] in {"id", "resourceType"}:
            raise ValueError("id와 resourceType은 Renderer가 identity로 관리합니다.")
        target, choices = _target_info(resource, path, schema)
        operation = mapping["operation"]
        if operation not in _target_operations(target, schema):
            raise ValueError(f"{path}의 FHIR 타입과 {operation} operation이 일치하지 않습니다.")
        if operation == "reference" and (
            mapping["resource_type"] not in schema["definitions"]
            or "resourceType" not in schema["definitions"][mapping["resource_type"]].get("properties", {})
        ):
            raise ValueError(f"FHIR schema에 없는 reference resource_type입니다: {mapping['resource_type']}")
        if operation == "reference" and metadata:
            nullable = _validate_reference_identity(mapping, metadata, location)
            if managed:
                required = _reference_path_required(resource, path, schema) or nullable is False
                policy = "error" if required else "omit"
                if policy != mapping.get("null_policy", "omit"):
                    reference_null_policies[index] = policy
        for old_path, old_choices, old_mapping in targets:
            conflicts = (
                path == old_path or path.startswith(old_path + ".") or old_path.startswith(path + ".")
                or any(group == old_group and variant != old_variant for group, variant in choices for old_group, old_variant in old_choices)
            )
            if conflicts and not conditions_disjoint(mapping.get("when"), old_mapping.get("when")):
                raise ValueError(f"겹치는 target_path 또는 FHIR choice 충돌입니다: {old_path}, {path}")
        targets.append((path, choices, mapping))
    result = deepcopy(profile)
    for index, policy in reference_null_policies.items():
        result["mappings"][index]["null_policy"] = policy
    return result


def _validate_mapping(mapping: Any, columns: set[str] | None, seen: set[str], location: str) -> None:
    if not isinstance(mapping, dict):
        raise ValueError(f"{location}는 객체여야 합니다.")
    mapping_id = str(mapping.get("id") or "")
    if not mapping_id or mapping_id in seen:
        raise ValueError(f"{location}.id가 없거나 중복된 mapping id입니다: {mapping_id}")
    seen.add(mapping_id)
    if not isinstance(mapping.get("target_path"), str) or not mapping["target_path"]:
        raise ValueError(f"{location}.target_path가 필요합니다.")
    operation = mapping.get("operation")
    if operation not in OPERATIONS:
        raise ValueError(f"{location}.operation이 허용 목록에 없습니다: {operation!r}")
    if mapping.get("null_policy", "omit") not in NULL_POLICIES:
        raise ValueError(f"{location}.null_policy가 허용되지 않습니다.")
    if "when" in mapping:
        _condition(mapping["when"], columns, f"{location}.when")
    if operation == "reference":
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]+", str(mapping.get("resource_type", ""))):
            raise ValueError(f"{location}.resource_type이 필요합니다.")
        if "identity" in mapping:
            _identity(mapping["identity"], columns, f"{location}.identity")
        else:
            _source_field(mapping.get("source_field"), columns, f"{location}.source_field")
    elif operation in {"coding", "codeable_concept", "identifier"}:
        names = ("system", "value") if operation == "identifier" else ("system", "code", "display")
        if not any(name in mapping for name in names):
            raise ValueError(f"{location}에는 {names} part가 필요합니다.")
        for name in names:
            if name in mapping:
                _validate_part(mapping[name], columns, f"{location}.{name}")
    else:
        _validate_part(mapping, columns, location)


def _validate_part(part: Any, columns: set[str] | None, location: str) -> None:
    if not isinstance(part, dict):
        raise ValueError(f"{location}는 객체여야 합니다.")
    operation = part.get("operation", "string")
    if operation not in OPERATIONS - {"coding", "codeable_concept", "identifier", "reference"}:
        raise ValueError(f"{location}.operation이 허용되지 않습니다: {operation!r}")
    if part.get("null_policy", "omit") not in NULL_POLICIES:
        raise ValueError(f"{location}.null_policy가 허용되지 않습니다.")
    if "when" in part:
        _condition(part["when"], columns, f"{location}.when")
    if operation == "constant":
        if "value" not in part:
            raise ValueError(f"{location}.value가 필요합니다.")
    else:
        _source_field(part.get("source_field"), columns, f"{location}.source_field")
    if operation == "lookup":
        if not isinstance(part.get("table"), dict):
            raise ValueError(f"{location}.table이 필요합니다.")
        if part.get("fallback", "source") not in {"source", "omit", "error"}:
            raise ValueError(f"{location}.fallback이 허용되지 않습니다.")


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        found = FORBIDDEN_KEYS.intersection(str(key).lower() for key in value)
        if found:
            raise ValueError("Conversion Profile에는 SQL 또는 실행 코드를 포함할 수 없습니다: " + ", ".join(sorted(found)))
        for nested in value.values():
            _reject_forbidden_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_keys(nested)
