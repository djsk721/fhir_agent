from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from .resource_definition import ordered_definitions


def build_bundle(
    resources: Iterable[dict[str, Any]], namespace: str = "fhir-source"
) -> dict[str, Any]:
    """생성된 리소스를 안정적인 fullUrl을 가진 FHIR collection Bundle로 묶습니다."""

    resource_list = list(resources)
    full_urls: dict[str, str] = {}
    for resource in resource_list:
        resource_type = str(resource.get("resourceType", "Resource"))
        resource_id = resource.get("id", resource_type.lower())
        key = f"{resource_type}/{resource_id}"
        if key in full_urls:
            raise ValueError(f"Bundle 리소스 ID가 중복됩니다: {key}")
        identity = f"{namespace}:{resource_type}:{resource_id}"
        full_urls[key] = f"urn:uuid:{uuid5(NAMESPACE_URL, identity)}"

    def rewrite_references(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("reference") in full_urls:
                value["reference"] = full_urls[value["reference"]]
            for nested in value.values():
                rewrite_references(nested)
        elif isinstance(value, list):
            for nested in value:
                rewrite_references(nested)

    entries: list[dict[str, Any]] = []
    for resource in resource_list:
        resource_type = str(resource.get("resourceType", "Resource"))
        resource_id = resource.get("id", resource_type.lower())
        bundled_resource = deepcopy(resource)
        rewrite_references(bundled_resource)
        entries.append(
            {
                "fullUrl": full_urls[f"{resource_type}/{resource_id}"],
                "resource": bundled_resource,
            }
        )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def build_bundle_pages(
    resources: Iterable[dict[str, Any]],
    *,
    namespace: str = "fhir-source",
    observation_page_size: int | None = None,
) -> list[dict[str, Any]]:
    """기본 리소스와 대량 Observation을 독립 Bundle page로 분리합니다."""

    paginated = [
        definition
        for definition in ordered_definitions()
        if definition.pagination_enabled
    ]
    if len(paginated) != 1:
        raise ValueError("페이지 분할 대상 FHIR 리소스는 정확히 하나여야 합니다.")
    definition = paginated[0]
    page_size = observation_page_size or definition.page_size
    if page_size < 1:
        raise ValueError("observation_page_size는 1 이상이어야 합니다.")
    resource_list = list(resources)
    base = [
        resource
        for resource in resource_list
        if resource.get("resourceType") != definition.resource_type
    ]
    observations = [
        resource
        for resource in resource_list
        if resource.get("resourceType") == definition.resource_type
    ]
    pages = [build_bundle(base, namespace=namespace)]
    pages.extend(
        build_bundle(
            observations[start : start + page_size],
            namespace=namespace,
        )
        for start in range(0, len(observations), page_size)
    )
    return pages
