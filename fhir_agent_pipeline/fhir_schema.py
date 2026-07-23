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
