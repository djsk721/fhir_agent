from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class ResourceCorrectionResult:
    corrected_resource: dict[str, Any]
    corrections: list[dict[str, str]]


@dataclass(frozen=True)
class BundleCorrectionResult:
    corrected_bundle: dict[str, Any]
    corrected_resources: dict[str, Any]
    corrections: list[dict[str, str]]


_REMOVE = object()
_VALUE_KEY_PATTERN = re.compile(r"^value[A-Z].+")


def _schema_default_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "db" / "fhir.schema.json")


@lru_cache(maxsize=4)
def load_fhir_schema(schema_path: str | None = None) -> dict[str, Any]:
    resolved_path = Path(schema_path or _schema_default_path()).resolve()
    with resolved_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def correct_fhir_resource(
    resource: dict[str, Any],
    resource_type: str,
    schema_path: str | None = None,
) -> ResourceCorrectionResult:
    if not isinstance(resource, dict):
        return ResourceCorrectionResult(corrected_resource={}, corrections=[])

    schema_document = load_fhir_schema(schema_path)
    corrections: list[dict[str, str]] = []
    resource_copy = dict(resource)

    current_resource_type = resource_copy.get("resourceType")
    if current_resource_type in (None, ""):
        resource_copy["resourceType"] = resource_type
        _record_correction(
            corrections,
            "resourceType",
            "resourceType was missing.",
            "fixed",
            "Filled in the expected FHIR resource type.",
        )

    definition = schema_document.get("definitions", {}).get(resource_type)
    if not isinstance(definition, dict):
        return ResourceCorrectionResult(corrected_resource=resource_copy, corrections=corrections)

    corrected = _correct_node(
        value=resource_copy,
        schema=definition,
        schema_document=schema_document,
        path="",
        corrections=corrections,
        root_resource_type=resource_type,
        is_root=True,
    )
    if not isinstance(corrected, dict):
        corrected = {}
    if current_resource_type not in (None, ""):
        corrected["resourceType"] = str(current_resource_type)
    elif corrected.get("resourceType") in (None, ""):
        corrected["resourceType"] = resource_type
    return ResourceCorrectionResult(corrected_resource=corrected, corrections=corrections)


def correct_fhir_bundle(
    bundle: dict[str, Any],
    schema_path: str | None = None,
) -> BundleCorrectionResult:
    schema_document = load_fhir_schema(schema_path)
    corrections: list[dict[str, str]] = []
    bundle_definition = schema_document.get("definitions", {}).get("Bundle")
    corrected_bundle = (
        _correct_node(
            value=dict(bundle) if isinstance(bundle, dict) else {},
            schema=bundle_definition,
            schema_document=schema_document,
            path="",
            corrections=corrections,
            root_resource_type="Bundle",
            is_root=True,
        )
        if isinstance(bundle_definition, dict)
        else dict(bundle)
        if isinstance(bundle, dict)
        else {"resourceType": "Bundle", "type": "collection", "entry": []}
    )
    if not isinstance(corrected_bundle, dict):
        corrected_bundle = {"resourceType": "Bundle", "type": "collection", "entry": []}

    corrected_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(corrected_bundle.get("entry", [])):
        entry_path = f"entry[{index}]"
        if not isinstance(entry, dict):
            _record_correction(
                corrections,
                entry_path,
                "Bundle entry was not an object.",
                "removed",
                "Removed the invalid Bundle entry.",
            )
            continue

        resource = entry.get("resource")
        if not isinstance(resource, dict):
            _record_correction(
                corrections,
                f"{entry_path}.resource",
                "Bundle entry resource was missing or not an object.",
                "removed",
                "Removed the invalid Bundle entry.",
            )
            continue

        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str) or not resource_type.strip():
            _record_correction(
                corrections,
                f"{entry_path}.resource.resourceType",
                "Bundle entry resourceType was missing.",
                "removed",
                "Removed the Bundle entry because its resource type could not be determined.",
            )
            continue

        resource_result = correct_fhir_resource(resource, resource_type, schema_path)
        corrections.extend(resource_result.corrections)
        corrected_entry: dict[str, Any] = dict(entry)
        corrected_entry["resource"] = resource_result.corrected_resource
        corrected_entries.append(corrected_entry)

    corrected_bundle["entry"] = corrected_entries
    _repair_bundle_references(corrected_bundle, corrections)
    corrected_resources = _resources_from_bundle(corrected_bundle)
    return BundleCorrectionResult(
        corrected_bundle=corrected_bundle,
        corrected_resources=corrected_resources,
        corrections=corrections,
    )


def _correct_node(
    value: Any,
    schema: dict[str, Any] | None,
    schema_document: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
    root_resource_type: str,
    is_root: bool = False,
) -> Any:
    if not isinstance(schema, dict):
        return value

    resolved_schema, schema_name = _resolve_schema(schema, schema_document)
    if resolved_schema is None:
        return value

    if schema_name == "CodeableConcept" and isinstance(value, dict):
        value = _promote_codeable_concept_coding(value, path, corrections)

    if "oneOf" in resolved_schema and isinstance(resolved_schema["oneOf"], list):
        return _correct_one_of(
            value,
            resolved_schema["oneOf"],
            schema_document,
            path,
            corrections,
            root_resource_type,
            is_root,
        )

    schema_type = resolved_schema.get("type")
    if schema_type == "array":
        if not isinstance(value, list):
            corrected_item = _correct_node(
                value,
                resolved_schema.get("items") if isinstance(resolved_schema.get("items"), dict) else None,
                schema_document,
                f"{path}[0]" if path else "[0]",
                [],
                root_resource_type,
            )
            if corrected_item is _REMOVE:
                return _REMOVE
            _record_correction(
                corrections,
                path or "array",
                "Field expected an array but received a single value.",
                "fixed",
                "Wrapped the single valid value in an array to match the FHIR schema.",
            )
            return [
                _correct_node(
                    value,
                    resolved_schema.get("items") if isinstance(resolved_schema.get("items"), dict) else None,
                    schema_document,
                    f"{path}[0]" if path else "[0]",
                    corrections,
                    root_resource_type,
                )
            ]
        corrected_items: list[Any] = []
        item_schema = resolved_schema.get("items")
        for index, item in enumerate(value):
            corrected_item = _correct_node(
                item,
                item_schema,
                schema_document,
                f"{path}[{index}]" if path else f"[{index}]",
                corrections,
                root_resource_type,
            )
            if corrected_item is _REMOVE:
                _record_correction(
                    corrections,
                    f"{path}[{index}]" if path else f"[{index}]",
                    "Array item did not match the FHIR schema.",
                    "removed",
                    "Removed the invalid array item.",
                )
                continue
            corrected_items.append(corrected_item)
        return corrected_items

    if schema_type == "object" or "properties" in resolved_schema or resolved_schema.get("additionalProperties") is False:
        if not isinstance(value, dict):
            return _REMOVE

        properties = resolved_schema.get("properties", {})
        corrected_object: dict[str, Any] = {}
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else key
            property_schema = properties.get(key)
            if property_schema is None and resolved_schema.get("additionalProperties") is False:
                _record_correction(
                    corrections,
                    item_path,
                    "Field is not defined in the FHIR schema.",
                    "removed",
                    "Removed the non-standard field.",
                )
                continue

            corrected_value = _correct_node(
                item,
                property_schema if isinstance(property_schema, dict) else None,
                schema_document,
                item_path,
                corrections,
                root_resource_type,
            )
            if corrected_value is _REMOVE:
                _record_correction(
                    corrections,
                    item_path,
                    "Field value did not match the FHIR schema.",
                    "removed",
                    "Removed the invalid field value.",
                )
                continue
            corrected_object[key] = corrected_value

        corrected_object = _apply_object_special_rules(
            corrected_object,
            schema_name,
            path,
            corrections,
        )
        if not corrected_object and not is_root:
            return _REMOVE
        return corrected_object

    return value if _matches_primitive_schema(value, resolved_schema) else _REMOVE


def _correct_one_of(
    value: Any,
    variants: list[Any],
    schema_document: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
    root_resource_type: str,
    is_root: bool,
) -> Any:
    if isinstance(value, dict):
        resource_type = value.get("resourceType")
        if isinstance(resource_type, str):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                _, schema_name = _resolve_schema(variant, schema_document)
                if schema_name == resource_type:
                    return _correct_node(
                        value,
                        variant,
                        schema_document,
                        path,
                        corrections,
                        root_resource_type,
                        is_root,
                    )

    for variant in variants:
        if not isinstance(variant, dict):
            continue
        corrected = _correct_node(
            value,
            variant,
            schema_document,
            path,
            [],
            root_resource_type,
            is_root,
        )
        if corrected is not _REMOVE:
            return _correct_node(
                value,
                variant,
                schema_document,
                path,
                corrections,
                root_resource_type,
                is_root,
            )
    return _REMOVE


def _apply_object_special_rules(
    value: dict[str, Any],
    schema_name: str | None,
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    if schema_name == "CodeableConcept":
        return _cleanup_codeable_concept(value, path, corrections)
    if schema_name == "Extension":
        return _cleanup_extension(value, path, corrections)
    if schema_name == "Reference":
        return _cleanup_reference(value, path, corrections)
    if schema_name == "Period":
        return _cleanup_period(value, path, corrections)
    return value


def _cleanup_codeable_concept(
    value: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    coding_value = value.get("coding")
    if isinstance(coding_value, list):
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for index, coding in enumerate(coding_value):
            if not isinstance(coding, dict):
                _record_correction(
                    corrections,
                    f"{path}.coding[{index}]" if path else f"coding[{index}]",
                    "Coding entry was not an object.",
                    "removed",
                    "Removed the invalid coding entry.",
                )
                continue
            code = coding.get("code")
            if not isinstance(code, str) or not code.strip():
                _record_correction(
                    corrections,
                    f"{path}.coding[{index}]" if path else f"coding[{index}]",
                    "Coding entry did not contain a usable code.",
                    "removed",
                    "Removed the invalid coding entry.",
                )
                continue
            identity = (
                str(coding.get("system") or ""),
                code.strip(),
                str(coding.get("display") or ""),
            )
            if identity in seen:
                _record_correction(
                    corrections,
                    f"{path}.coding[{index}]" if path else f"coding[{index}]",
                    "Duplicate coding entry.",
                    "removed",
                    "Removed the duplicate coding entry.",
                )
                continue
            seen.add(identity)
            deduped.append(coding)
        if deduped:
            value["coding"] = deduped
        else:
            value.pop("coding", None)

    if not value:
        _record_correction(
            corrections,
            path or "CodeableConcept",
            "CodeableConcept became empty after cleanup.",
            "removed",
            "Removed the empty CodeableConcept.",
        )
    return value


def _cleanup_extension(
    value: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    url = value.get("url")
    if not isinstance(url, str) or not url.strip():
        _record_correction(
            corrections,
            path or "extension",
            "Extension.url was missing.",
            "removed",
            "Removed the malformed extension.",
        )
        return {}

    nested_extensions = value.get("extension")
    has_nested_extensions = isinstance(nested_extensions, list) and bool(nested_extensions)
    value_keys = [key for key in value if _VALUE_KEY_PATTERN.match(key)]
    if value_keys and has_nested_extensions:
        _record_correction(
            corrections,
            path or "extension",
            "Extension contained both value[x] and nested extension.",
            "removed",
            "Removed the malformed extension instead of guessing which representation was intended.",
        )
        return {}

    if not has_nested_extensions and not value_keys:
        _record_correction(
            corrections,
            path or "extension",
            "Extension did not contain a value[x] or nested extension.",
            "removed",
            "Removed the malformed extension.",
        )
        return {}
    return value


def _cleanup_reference(
    value: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    if any(
        isinstance(value.get(key), str) and str(value.get(key)).strip()
        for key in ("reference", "display")
    ) or isinstance(value.get("identifier"), dict):
        return value
    if value.get("extension"):
        return value
    _record_correction(
        corrections,
        path or "Reference",
        "Reference did not contain reference, identifier, or display.",
        "removed",
        "Removed the empty reference object.",
    )
    return {}


def _cleanup_period(
    value: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    start = value.get("start")
    end = value.get("end")
    start_dt = _parse_full_datetime(start)
    end_dt = _parse_full_datetime(end)
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        _record_correction(
            corrections,
            path or "Period",
            "Period.start was later than Period.end.",
            "removed",
            "Removed the conflicting Period because its chronology was invalid.",
        )
        return {}
    return value


def _repair_bundle_references(
    bundle: dict[str, Any],
    corrections: list[dict[str, str]],
) -> None:
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        bundle["entry"] = []
        return

    patient_ids = [
        str(entry.get("resource", {}).get("id"))
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == "Patient"
        and entry["resource"].get("id") is not None
    ]
    canonical_patient_id = patient_ids[0] if len(set(patient_ids)) == 1 and patient_ids else None
    encounter_ids = [
        str(entry.get("resource", {}).get("id"))
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("resource"), dict)
        and entry["resource"].get("resourceType") == "Encounter"
        and entry["resource"].get("id") is not None
    ]
    canonical_encounter_id = encounter_ids[0] if len(set(encounter_ids)) == 1 and encounter_ids else None

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")
        if isinstance(resource_type, str) and resource_id is not None:
            expected_full_url = f"urn:resource:{resource_type.lower()}:{resource_id}"
            if entry.get("fullUrl") != expected_full_url:
                entry["fullUrl"] = expected_full_url
                _record_correction(
                    corrections,
                    f"entry[{index}].fullUrl",
                    "Bundle entry fullUrl was missing or inconsistent with the resource id.",
                    "fixed",
                    "Normalized the Bundle fullUrl to match the contained resource.",
                )

        if canonical_patient_id is not None:
            subject = resource.get("subject")
            if isinstance(subject, dict):
                reference = subject.get("reference")
                expected_reference = f"Patient/{canonical_patient_id}"
                if isinstance(reference, str) and reference != expected_reference and reference.startswith("Patient/"):
                    subject["reference"] = expected_reference
                    _record_correction(
                        corrections,
                        f"entry[{index}].resource.subject.reference",
                        "Subject reference was inconsistent with the Bundle patient.",
                        "fixed",
                        "Rewrote the subject reference to the canonical Patient in the Bundle.",
                    )

        if resource_type == "Observation" and canonical_encounter_id is not None:
            encounter = resource.get("encounter")
            if isinstance(encounter, dict):
                reference = encounter.get("reference")
                expected_reference = f"Encounter/{canonical_encounter_id}"
                if isinstance(reference, str) and reference != expected_reference and reference.startswith("Encounter/"):
                    encounter["reference"] = expected_reference
                    _record_correction(
                        corrections,
                        f"entry[{index}].resource.encounter.reference",
                        "Encounter reference was inconsistent with the Bundle encounter.",
                        "fixed",
                        "Rewrote the encounter reference to the canonical Encounter in the Bundle.",
                    )


def _resources_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in bundle.get("entry", []):
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str):
            continue
        grouped.setdefault(resource_type, []).append(resource)

    return {
        resource_type: resources[0] if len(resources) == 1 else resources
        for resource_type, resources in grouped.items()
    }


def _resolve_schema(
    schema: dict[str, Any],
    schema_document: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        schema_name = ref.rsplit("/", 1)[-1]
        return schema_document.get("definitions", {}).get(schema_name), schema_name
    return schema, None


def _promote_codeable_concept_coding(
    value: dict[str, Any],
    path: str,
    corrections: list[dict[str, str]],
) -> dict[str, Any]:
    if "coding" in value:
        return dict(value)

    code = value.get("code")
    if not isinstance(code, str) or not code.strip():
        return dict(value)

    coding: dict[str, Any] = {"code": code}
    for key in ("system", "display"):
        nested_value = value.get(key)
        if isinstance(nested_value, str) and nested_value.strip():
            coding[key] = nested_value

    promoted = dict(value)
    promoted["coding"] = [coding]
    for key in ("system", "code", "display"):
        if key in promoted:
            promoted.pop(key, None)
    _record_correction(
        corrections,
        path or "CodeableConcept",
        "CodeableConcept used Coding keys at the wrong level.",
        "fixed",
        "Moved system/code/display into CodeableConcept.coding[0].",
    )
    return promoted


def _matches_primitive_schema(value: Any, schema: dict[str, Any]) -> bool:
    expected_type = schema.get("type")
    if expected_type == "string":
        if not isinstance(value, str) or not value:
            return False
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return False
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False

    const_value = schema.get("const")
    if const_value is not None and value != const_value:
        return False

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        return False

    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        if not isinstance(value, str):
            return False
        if re.match(pattern, value) is None:
            return False
    return True


def _parse_full_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _record_correction(
    corrections: list[dict[str, str]],
    field: str,
    issue: str,
    action: str,
    reason: str,
) -> None:
    correction = {
        "field": field,
        "issue": issue,
        "action": action,
        "reason": reason,
    }
    if correction not in corrections:
        corrections.append(correction)
