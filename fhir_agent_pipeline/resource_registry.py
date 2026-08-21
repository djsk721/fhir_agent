from __future__ import annotations

from typing import Any

from .resource_definition import (
    RESOURCE_DEFINITIONS,
    ResourceDefinition,
    definition_for,
    ordered_definitions,
    supported_resource_types,
)


# 기존 import 경로와 외부 호출 규약을 유지하되 정의는 한 곳만 사용합니다.
ResourceAdapter = ResourceDefinition
RESOURCE_ADAPTERS: tuple[ResourceDefinition, ...] = tuple(
    ordered_definitions()
)


def selected_adapters(
    resource_types: list[str],
) -> list[ResourceDefinition]:
    return ordered_definitions(resource_types)


def adapter_for(resource_type: str) -> ResourceDefinition:
    return definition_for(resource_type)


def required_bundle_resource_types(resource_types: list[str]) -> list[str]:
    return [
        definition.resource_type
        for definition in ordered_definitions(resource_types)
        if definition.bundle_required
    ]


def resource_semantic_errors(resource: dict[str, Any]) -> list[str]:
    """JSON Schema가 빠뜨린 현재 Task의 필수 최상위 필드를 검사합니다."""

    resource_type = str(resource.get("resourceType", ""))
    try:
        definition = definition_for(resource_type)
    except ValueError:
        return [f"지원하지 않는 resourceType입니다: {resource_type!r}"]
    missing = [
        field_name
        for field_name in definition.required_fields
        if resource.get(field_name) in (None, "")
    ]
    return [
        f"{resource_type}.{field_name} 필수값이 없습니다."
        for field_name in missing
    ]
