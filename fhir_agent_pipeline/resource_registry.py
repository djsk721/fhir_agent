from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceAdapter:
    """LLM 변환 대상의 순서, 검증 모델, 의존성을 한 곳에 정의합니다."""

    resource_type: str
    order: int
    dependencies: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    source_required: bool = False
    bundle_required: bool = False


RESOURCE_ADAPTERS: tuple[ResourceAdapter, ...] = (
    ResourceAdapter(
        "Patient",
        0,
        required_fields=("id",),
        source_required=True,
        bundle_required=True,
    ),
    ResourceAdapter(
        "Encounter",
        10,
        ("Patient",),
        ("id", "status", "class", "subject"),
        source_required=True,
        bundle_required=True,
    ),
    ResourceAdapter(
        "Condition",
        20,
        ("Patient", "Encounter"),
        ("id", "code", "subject"),
    ),
    ResourceAdapter(
        "Procedure",
        30,
        ("Patient", "Encounter"),
        ("id", "status", "code", "subject"),
    ),
    ResourceAdapter(
        "Observation",
        40,
        ("Patient", "Encounter"),
        ("id", "status", "code", "subject"),
    ),
)


def supported_resource_types() -> set[str]:
    return {adapter.resource_type for adapter in RESOURCE_ADAPTERS}


def selected_adapters(resource_types: list[str]) -> list[ResourceAdapter]:
    selected = set(resource_types)
    return sorted(
        (adapter for adapter in RESOURCE_ADAPTERS if adapter.resource_type in selected),
        key=lambda adapter: adapter.order,
    )


def adapter_for(resource_type: str) -> ResourceAdapter:
    try:
        return next(
            adapter
            for adapter in RESOURCE_ADAPTERS
            if adapter.resource_type == resource_type
        )
    except StopIteration as exc:
        raise ValueError(
            f"지원하지 않는 resourceType입니다: {resource_type!r}"
        ) from exc


def required_bundle_resource_types(resource_types: list[str]) -> list[str]:
    return [
        adapter.resource_type
        for adapter in selected_adapters(resource_types)
        if adapter.bundle_required
    ]


def resource_semantic_errors(resource: dict[str, Any]) -> list[str]:
    """JSON Schema가 빠뜨린 현재 Task의 필수 최상위 필드를 검사합니다."""

    resource_type = str(resource.get("resourceType", ""))
    try:
        adapter = adapter_for(resource_type)
    except ValueError:
        return [f"지원하지 않는 resourceType입니다: {resource_type!r}"]
    missing = [
        field_name
        for field_name in adapter.required_fields
        if resource.get(field_name) in (None, "")
    ]
    return [
        f"{resource_type}.{field_name} 필수값이 없습니다."
        for field_name in missing
    ]
