from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft6Validator


class FhirSchemaRepository:
    """FHIR JSON Schema를 한 번만 로드하고 리소스별 Validator를 재사용합니다."""

    def __init__(self, schema_path: Path) -> None:
        self.schema_path = schema_path
        if not schema_path.is_file():
            raise FileNotFoundError(f"FHIR_SCHEMA_PATH 파일을 찾을 수 없습니다: {schema_path}")
        with schema_path.open(encoding="utf-8") as handle:
            self.definitions: dict[str, Any] = json.load(handle).get("definitions", {})
        self._validators: dict[str, Draft6Validator] = {}

    def definition(self, resource_type: str) -> dict[str, Any]:
        return self.definitions.get(resource_type, {})

    def property_schema(
        self,
        resource_type: str,
        property_name: str,
    ) -> dict[str, Any] | None:
        """리소스 속성의 공식 JSON Schema 정의를 반환합니다."""
        value = self.definition(resource_type).get("properties", {}).get(property_name)
        return value if isinstance(value, dict) else None

    def coerce_source_value(
        self,
        property_schema: dict[str, Any],
        value: Any,
    ) -> Any:
        """원천 scalar/list를 FHIR Schema가 요구하는 외형으로만 변환합니다.

        코드나 의미를 추론하지 않고 array 여부와 `text`를 지원하는 FHIR
        complex datatype의 컨테이너만 보정합니다.
        """
        if value in (None, ""):
            return None
        if property_schema.get("type") == "array":
            values = value if isinstance(value, list) else [value]
            item_schema = property_schema.get("items", {})
            return [
                self.coerce_source_value(item_schema, item)
                for item in values
                if item not in (None, "")
            ]
        reference = property_schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/definitions/"):
            definition_name = reference.rsplit("/", 1)[-1]
            definition = self.definitions.get(definition_name, {})
            if isinstance(value, dict):
                return value
            if "text" in definition.get("properties", {}):
                return {"text": str(value)}
            return self.coerce_source_value(definition, value)
        value_type = property_schema.get("type")
        if value_type == "string":
            return str(value)
        if value_type == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if value_type == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if value_type == "boolean" and isinstance(value, bool):
            return value
        return value

    def errors(self, resource: dict[str, Any]) -> list[str]:
        resource_type = str(resource.get("resourceType", ""))
        if resource_type not in self.definitions:
            return [f"FHIR JSON Schema에 정의되지 않은 resourceType입니다: {resource_type}"]
        validator = self._validators.get(resource_type)
        if validator is None:
            validator = Draft6Validator({
                "$schema": "http://json-schema.org/draft-06/schema#",
                "$ref": f"#/definitions/{resource_type}",
                "definitions": self.definitions,
            })
            self._validators[resource_type] = validator
        errors = []
        for error in validator.iter_errors(resource):
            path = ".".join(str(item) for item in error.absolute_path)
            errors.append(f"JSON Schema {path or resource_type}: {error.message}")
        return errors


@lru_cache(maxsize=4)
def get_fhir_schema(schema_path: str | Path) -> FhirSchemaRepository:
    return FhirSchemaRepository(Path(schema_path).expanduser().resolve())
