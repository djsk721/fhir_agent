"""Local LLM의 출력 구조를 제한하는 데이터셋 독립 JSON Schema."""

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import re

from .schema import (
    OPERATIONS, SUPPORTED_RESOURCES, DEFAULT_SCHEMA_PATH, _columns, _resolve, _schema,
    _target_operations, canonical_identity,
)


# Bound model-generated path complexity without naming any source fields. The
# renderer's FHIR resources do not support model-created contained resources or
# extension payloads in this baseline. Array indices are bounded before render.
_MAX_PATH_SEGMENTS = 6
_ARRAY_SEGMENT = "[index]"
_ARRAY_INDEX_PATTERN = "([0-9]|[1-5][0-9]|6[0-3])"
_EXCLUDED_CHILDREN = {"extension", "modifierExtension", "contained"}
_MAX_PATTERN_CHARS = 36000


@lru_cache(maxsize=32)
def _target_catalog(schema_path: str, resource: str) -> dict[str, frozenset[tuple[str, ...]]]:
    schema = _schema(schema_path)
    definition = schema["definitions"].get(resource)
    if not definition:
        raise ValueError(f"FHIR schema에 없는 target_resource입니다: {resource}")
    paths = defaultdict(set)

    def walk(node, parts=(), ancestry=frozenset()):
        reference = node.get("$ref")
        if reference in ancestry:
            return
        if reference:
            ancestry = ancestry | {reference}
        actual = _resolve(node, schema)
        if parts:
            for operation in _target_operations(actual, schema):
                paths[operation].add(parts)
        if len(parts) == _MAX_PATH_SEGMENTS:
            return
        if actual.get("type") == "array":
            walk(actual["items"], (*parts, _ARRAY_SEGMENT), ancestry)
        else:
            for name, child in actual.get("properties", {}).items():
                if ((not parts and name in {"id", "resourceType"})
                        or name.startswith("_") or name in _EXCLUDED_CHILDREN):
                    continue
                walk(child, (*parts, name), ancestry)

    walk(definition)
    return {operation: frozenset(allowed) for operation, allowed in paths.items()}


def _path_alternatives(paths: frozenset[tuple[str, ...]]) -> str:
    """Factor common path prefixes to keep Ollama's regex grammar compact."""
    def group(alternatives):
        return alternatives[0] if len(alternatives) == 1 else "(" + "|".join(alternatives) + ")"

    def tree(sequences):
        children = defaultdict(list)
        for path in sequences:
            children[path[0]].append(path[1:])
        expressions = []
        for part, remaining in sorted(children.items()):
            literal = _ARRAY_INDEX_PATTERN if part == _ARRAY_SEGMENT else re.escape(part)
            nonempty = [path for path in remaining if path]
            if nonempty:
                suffix = r"\." + tree(nonempty)
                literal += "(" + suffix + ")?" if len(nonempty) < len(remaining) else suffix
            expressions.append(literal)
        return group(expressions)

    return tree(paths)


def _path_schema(paths: frozenset[tuple[str, ...]]) -> dict:
    if not paths:
        return {"not": {}}
    return {"type": "string", "pattern": "^" + _path_alternatives(paths) + "$"}


def _target_rule_pattern(catalog):
    by_paths = defaultdict(list)
    for operation, paths in sorted(catalog.items()):
        by_paths[paths].append(operation)
    alternatives = []
    for paths, operations in by_paths.items():
        prefix = operations[0] if len(operations) == 1 else "(" + "|".join(operations) + ")"
        alternatives.append(prefix + ":" + _path_alternatives(paths))
    pattern = "^(" + "|".join(alternatives) + ")$"
    if len(pattern) > _MAX_PATTERN_CHARS:
        raise ValueError("FHIR target_rule 생성 grammar가 안전한 크기를 초과했습니다.")
    return pattern


def unpack_target_rules(payload: dict) -> dict:
    """Convert the provider wire representation to the existing Profile format."""
    if not isinstance(payload, dict) or not isinstance(payload.get("mappings"), list):
        return payload
    normalized = []
    for mapping in payload["mappings"]:
        if not isinstance(mapping, dict) or "target_rule" not in mapping:
            normalized.append(mapping)
            continue
        rule = mapping["target_rule"]
        if (not isinstance(rule, str) or rule.count(":") != 1
                or {"operation", "target_path"} & mapping.keys()):
            raise ValueError("target_rule은 operation:target_path 형식이어야 합니다.")
        operation, _, path = rule.partition(":")
        normalized.append({
            **{key: value for key, value in mapping.items() if key != "target_rule"},
            "operation": operation, "target_path": path,
        })
    return {**payload, "mappings": normalized}


def profile_output_schema(target_resource=None, metadata=None, schema_path=DEFAULT_SCHEMA_PATH) -> dict:
    columns = _columns(metadata)
    source_field = {"type": "string"}
    if columns:
        source_field["enum"] = sorted(columns)
    catalog = _target_catalog(str(Path(schema_path).resolve()), target_resource) if target_resource else None

    def target_path(operation):
        return _path_schema(catalog.get(operation, frozenset())) if catalog else {"type": "string"}
    condition = {"anyOf": [
        {"type": "object", "properties": {"field": source_field, operator: value},
         "required": ["field", operator], "additionalProperties": False}
        for operator, value in (
            ("equals", {}), ("in", {"type": "array", "minItems": 1}), ("present", {"type": "boolean"}),
        )
    ]}
    for operator in ("all", "any", "not"):
        value = {"$ref": "#/$defs/condition"}
        if operator != "not":
            value = {"type": "array", "items": value, "minItems": 1}
        condition["anyOf"].append({"type": "object", "properties": {operator: value},
                                   "required": [operator], "additionalProperties": False})
    identity = {"type": "object", "properties": {
        "source_fields": {"type": "array", "items": source_field, "minItems": 1},
        "separator": {"type": "string"}, "prefix": {"type": "string"},
    }, "required": ["source_fields"], "additionalProperties": False}
    common = {"null_policy": {"enum": ["omit", "null", "error"]},
              "when": {"$ref": "#/$defs/condition"}, "timezone": {"type": "string"}}
    parts = []
    for operation in sorted(OPERATIONS - {"reference", "coding", "codeable_concept", "identifier"}):
        properties = {**common, "operation": {"enum": [operation]}}
        required = ["operation"]
        if operation == "constant":
            properties["value"] = {}
            required.append("value")
        else:
            properties["source_field"] = source_field
            required.append("source_field")
        if operation == "lookup":
            properties.update(table={"type": "object"}, fallback={"enum": ["source", "omit", "error"]})
            required.append("table")
        parts.append({"type": "object", "properties": properties, "required": required, "additionalProperties": False})
    mappings = []
    for part in parts:
        operation = part["properties"]["operation"]["enum"][0]
        mappings.append({**part, "properties": {**part["properties"], "id": {"type": "string"}, "target_path": target_path(operation)},
                         "required": [*part["required"], "id", "target_path"]})
    reference = {"type": "object", "properties": {
        **common, "id": {"type": "string"}, "target_path": target_path("reference"),
        "operation": {"enum": ["reference"]}, "resource_type": {"type": "string"},
        "source_field": source_field, "identity": identity,
    }, "required": ["id", "target_path", "operation", "resource_type"],
        "anyOf": [{"required": ["source_field"]}, {"required": ["identity"]}], "additionalProperties": False}
    if metadata and metadata.get("related_sources"):
        reference["properties"]["resource_type"]["enum"] = sorted(metadata["related_sources"])
    mappings.append(reference)
    for operation in ("coding", "codeable_concept", "identifier"):
        names = ("system", "value") if operation == "identifier" else ("system", "code", "display")
        mappings.append({"type": "object", "properties": {
            **common, "id": {"type": "string"}, "target_path": target_path(operation),
            "operation": {"enum": [operation]}, **{name: {"$ref": "#/$defs/part"} for name in names},
        }, "required": ["id", "target_path", "operation"],
            "anyOf": [{"required": [name]} for name in names], "additionalProperties": False})
    source = {name: {"type": "string"} for name in ("dataset", "table")}
    for name in source:
        if metadata and metadata.get(name):
            source[name]["enum"] = [metadata[name]]
    root_identity = identity
    identity_contract = canonical_identity(metadata)
    if fields := (identity_contract or {}).get("source_fields"):
        root_identity = {**identity, "properties": {
            "source_fields": {"type": "array", "items": {"type": "string", "enum": fields},
                              "minItems": len(fields), "maxItems": len(fields),
                              "const": fields},
        }}
    return {"type": "object", "$defs": {"part": {"anyOf": parts}, "condition": condition}, "properties": {
        "profile_id": {"type": "string"}, "profile_version": {"type": "string"},
        "target_resource": {"enum": [target_resource] if target_resource else list(SUPPORTED_RESOURCES)}, "fhir_version": {"enum": ["4.0.1", "R4"]},
        "source": {"type": "object", "properties": source,
                   "required": ["dataset", "table"], "additionalProperties": False},
        "identity": root_identity, "mappings": {"type": "array", "items": {"anyOf": mappings}, "minItems": 1},
    }, "required": ["profile_id", "profile_version", "target_resource", "fhir_version", "source", "identity", "mappings"],
        "additionalProperties": False}


def profile_generation_schema(target_resource=None, metadata=None, schema_path=DEFAULT_SCHEMA_PATH, *, wire=False) -> dict:
    """Ollama wire contracts couple operation and path without object unions."""
    schema = profile_output_schema(target_resource, metadata, schema_path)
    scalar = schema["$defs"]["part"]["anyOf"][0]["properties"]
    field = {"type": "string"}
    columns = _columns(metadata)
    if columns:
        field["enum"] = sorted(columns)
    json_value = {"type": ["string", "number", "boolean", "null", "object", "array"]}
    part = {"type": "object", "properties": {
        "operation": {"enum": sorted(OPERATIONS - {"coding", "codeable_concept", "identifier", "reference"})},
        "source_field": field, "value": json_value,
        "table": {"type": "object", "additionalProperties": json_value},
        "fallback": {"enum": ["source", "omit", "error"]},
        "null_policy": scalar["null_policy"], "timezone": {"type": "string"}, "when": {"type": "object"},
    }, "required": ["operation"], "additionalProperties": False}
    mapping = {"type": "object", "properties": {
        # Constrained decoders emit properties in schema order. Keep the required
        # header before optional source fields so selecting id/path cannot skip them.
        "id": {"type": "string"},
        "target_path": (
            _path_schema(frozenset().union(*_target_catalog(str(Path(schema_path).resolve()), target_resource).values()))
            if target_resource else {"type": "string"}
        ),
        "operation": {"enum": sorted(OPERATIONS)}, "resource_type": {"type": "string"},
        **{name: value for name, value in part["properties"].items() if name != "operation"},
        "identity": next(option for option in schema["properties"]["mappings"]["items"]["anyOf"]
                         if option["properties"]["operation"]["enum"] == ["reference"])["properties"]["identity"],
        **{name: part for name in ("system", "code", "display")},
    }, "required": ["id", "target_path", "operation"], "additionalProperties": False}
    if metadata and metadata.get("related_sources"):
        mapping["properties"]["resource_type"]["enum"] = sorted(metadata["related_sources"])
    if wire and target_resource:
        catalog = _target_catalog(str(Path(schema_path).resolve()), target_resource)
        props = mapping["properties"]
        mapping["properties"] = {
            "id": props["id"], "target_rule": {"type": "string", "pattern": _target_rule_pattern(catalog)},
            **{key: value for key, value in props.items() if key not in {"id", "target_path", "operation"}},
        }
        mapping["required"] = ["id", "target_rule"]
        if (metadata or {}).get("identity_contract"):
            schema["required"].remove("identity")
            del schema["properties"]["identity"]
    schema.pop("$defs")
    schema["properties"]["mappings"]["items"] = mapping
    return schema
