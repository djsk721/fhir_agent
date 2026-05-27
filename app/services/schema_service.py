from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.models.responses import SchemaResponse


class SchemaService:
    def __init__(self, schema_path: Path | None = None) -> None:
        self._schema_path = (
            schema_path
            or Path(__file__).resolve().parents[2] / "db" / "fhir.schema.json"
        )
        self._default_resource_types = ["Patient", "Encounter", "Observation", "Bundle"]

    @lru_cache(maxsize=1)
    def _load_schema_document(self) -> dict[str, Any]:
        with self._schema_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def get_schema(self, resource_types: list[str] | None = None) -> SchemaResponse:
        document = self._load_schema_document()
        definitions = document.get("definitions", {})
        selected_types = resource_types or self._default_resource_types

        missing = [
            resource_type
            for resource_type in selected_types
            if resource_type not in definitions
        ]
        if missing:
            raise HTTPException(
                status_code=404, detail=f"Unknown resource types: {', '.join(missing)}"
            )

        return SchemaResponse(
            source=str(self._schema_path),
            supported_resource_types=selected_types,
            definitions={
                resource_type: definitions[resource_type]
                for resource_type in selected_types
            },
        )

    def build_schema_context(
        self, resource_types: list[str] | None = None
    ) -> dict[str, Any]:
        schema = self.get_schema(resource_types)
        return {
            "source": schema.source,
            "resource_types": schema.supported_resource_types,
            "definition_count": len(schema.definitions),
        }
