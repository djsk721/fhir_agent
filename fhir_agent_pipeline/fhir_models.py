from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5


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
