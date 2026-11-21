from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any

from ..profiles.schema import canonical_identity, validate_profile


class Omitted:
    """원천 값이 없어 FHIR 요소를 생성하지 않는다는 표시입니다."""


OMIT = Omitted()


def parse_path(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for segment in path.split("."):
        if not segment:
            raise ValueError(f"비어 있는 FHIR path입니다: {path}")
        parts.append(int(segment) if segment.isdigit() else segment)
    return parts


def get_path(value: Any, path: str) -> Any:
    current = value
    for part in parse_path(path):
        if isinstance(part, int):
            if not isinstance(current, list) or len(current) <= part:
                return None
            current = current[part]
        elif not isinstance(current, dict):
            return None
        else:
            current = current.get(part)
    return current


def set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = parse_path(path)
    current: Any = target
    for part, following in zip(parts, parts[1:]):
        next_container: dict[str, Any] | list[Any] = [] if isinstance(following, int) else {}
        if isinstance(part, int):
            while len(current) <= part:
                current.append({})
            if not isinstance(current[part], (dict, list)):
                current[part] = next_container
            current = current[part]
        else:
            existing = current.get(part)
            if not isinstance(existing, type(next_container)):
                current[part] = next_container
            current = current[part]
    last = parts[-1]
    if isinstance(last, int):
        while len(current) <= last:
            current.append(None)
        current[last] = value
    else:
        current[last] = value


def prune(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {key: prune(nested) for key, nested in value.items()}
        return {
            key: nested
            for key, nested in cleaned.items()
            if nested not in (None, {}, [])
        }
    if isinstance(value, list):
        return [
            nested
            for nested in (prune(item) for item in value)
            if nested not in (None, {}, [])
        ]
    return value


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _missing_key(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decimal(value: Any) -> int | float:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"decimal로 변환할 수 없습니다: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"FHIR decimal은 유한한 값이어야 합니다: {value!r}")
    if number == number.to_integral():
        return int(number)
    result = float(number)
    if not math.isfinite(result):
        raise ValueError(f"FHIR decimal 범위를 초과했습니다: {value!r}")
    if result == 0 and number != 0:
        raise ValueError(f"FHIR decimal을 JSON number로 표현할 수 없습니다: {value!r}")
    return result


def _timezone_datetime(value: Any, timezone: str | None) -> str:
    text = str(value).strip().replace(" ", "T")
    # FHIR dateTime permits partial dates; do not invent midnight or a timezone.
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        _date(text)
        return text
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:0\d|1[0-3]):[0-5]\d|[+-]14:00)?",
        text,
    ):
        raise ValueError(f"FHIR datetime 형식이 아닙니다: {value!r}")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if timezone is None:
            raise ValueError("timezone 없는 datetime에는 명시적인 timezone 정책이 필요합니다.")
        if not re.fullmatch(r"Z|[+-](?:0\d|1[0-3]):[0-5]\d|[+-]14:00", timezone):
            raise ValueError(f"잘못된 timezone offset입니다: {timezone!r}")
        return text + timezone
    # Preserve original precision, fractional seconds and offset spelling.
    return text


def _date(value: Any) -> str:
    text = str(value).strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}", text):
        date(int(text), 1, 1)
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        date.fromisoformat(text + "-01")
        return text
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "0", "1"}:
        return value.strip().lower() in {"true", "1"}
    raise ValueError(f"boolean으로 변환할 수 없습니다: {value!r}")


def condition_matches(condition: dict[str, Any] | None, record: dict[str, Any]) -> bool:
    """eval 없이 선언적 조건만 판정합니다. 조건 비교는 타입을 보존합니다."""
    if condition is None:
        return True
    if "all" in condition:
        return all(condition_matches(part, record) for part in condition["all"])
    if "any" in condition:
        return any(condition_matches(part, record) for part in condition["any"])
    if "not" in condition:
        return not condition_matches(condition["not"], record)
    value = record.get(condition["field"])
    if "present" in condition:
        return (not _missing(value)) == condition["present"]
    if "equals" in condition:
        return type(value) is type(condition["equals"]) and value == condition["equals"]
    if "in" in condition:
        return any(type(value) is type(item) and value == item for item in condition["in"])
    raise ValueError("지원하지 않는 when 조건입니다.")


def apply_part(part: dict[str, Any], record: dict[str, Any], timezone: str | None = None) -> Any:
    if not condition_matches(part.get("when"), record):
        return OMIT
    operation = part.get("operation", "string")
    if operation == "constant":
        return deepcopy(part.get("value"))
    source = record.get(part.get("source_field"))
    if _missing(source):
        if part.get("null_policy", "omit") == "error":
            raise ValueError(f"원천 값이 없습니다: {part.get('source_field')}")
        return OMIT if part.get("null_policy", "omit") == "omit" else None
    if operation == "copy":
        return deepcopy(source)
    if operation == "string":
        return _text(source)
    if operation == "integer":
        number = _decimal(source)
        if isinstance(number, float):
            raise ValueError(f"소수 값은 integer로 변환할 수 없습니다: {source!r}")
        return number
    if operation == "decimal":
        return _decimal(source)
    if operation == "boolean":
        return _boolean(source)
    if operation == "date":
        return _date(source)
    if operation == "datetime":
        return _timezone_datetime(source, part.get("timezone", timezone))
    if operation == "lookup":
        table = {str(key): value for key, value in part.get("table", {}).items()}
        key = _text(source)
        if key in table:
            return table[key]
        fallback = part.get("fallback", "source")
        if fallback == "omit":
            return OMIT
        if fallback == "error":
            raise ValueError(f"lookup에 없는 값입니다: {key}")
        return key
    raise ValueError(f"지원하지 않는 part operation입니다: {operation}")


def apply_mapping(mapping: dict[str, Any], record: dict[str, Any], timezone: str | None = None) -> Any:
    """허용된 선언적 연산 하나를 원천 행에 적용합니다."""

    if not condition_matches(mapping.get("when"), record):
        return OMIT
    operation = mapping["operation"]
    if operation in {"coding", "codeable_concept", "identifier"}:
        coding = {}
        if mapping.get("system"):
            coding["system"] = apply_part(mapping["system"], record, timezone)
        if mapping.get("code"):
            coding["code"] = apply_part(mapping["code"], record, timezone)
        if mapping.get("display"):
            coding["display"] = apply_part(mapping["display"], record, timezone)
        coding = {key: value for key, value in coding.items() if value is not OMIT}
        if operation == "identifier":
            if not mapping.get("value"):
                return OMIT
            value = apply_part(mapping["value"], record, timezone)
            if value is OMIT:
                return OMIT
            identifier = {"value": value}
            if "system" in coding:
                identifier["system"] = coding["system"]
            return [identifier]
        if not any(key in coding for key in ("code", "display")):
            return OMIT
        if operation == "coding":
            return coding
        return {"coding": [coding]}

    if operation == "reference":
        identity = mapping.get("identity") or {"source_fields": [mapping["source_field"]]}
        missing = [_missing_key(record.get(field)) for field in identity["source_fields"]]
        if any(missing):
            if not all(missing):
                raise ValueError(f"{mapping['id']}의 복합 참조 키가 일부만 있습니다.")
            if mapping.get("null_policy", "omit") == "error":
                raise ValueError(f"{mapping['id']}의 참조 원천 값이 없습니다.")
            return OMIT if mapping.get("null_policy", "omit") == "omit" else None
        return {"reference": f"{mapping['resource_type']}/{resource_id({'identity': identity}, record)}"}
    return apply_part(mapping, record, timezone)


def resource_id(profile: dict[str, Any], record: dict[str, Any]) -> str:
    separator = str(profile["identity"].get("separator", "-"))
    parts = []
    for field_name in profile["identity"]["source_fields"]:
        value = record.get(field_name)
        if _missing_key(value):
            raise ValueError(f"리소스 ID 원천 값이 없습니다: {field_name}")
        if (isinstance(value, (list, tuple, dict, set, bool))
                or isinstance(value, float) and not math.isfinite(value)
                or isinstance(value, Decimal) and not value.is_finite()):
            raise ValueError(f"리소스 ID 원천 값은 유효한 스칼라여야 합니다: {field_name}")
        parts.append(_text(value))
    prefix = str(profile["identity"].get("prefix", ""))
    candidate = prefix + separator.join(parts)
    # A composite separator inside a component makes joining ambiguous. Hash the
    # structured components in that case, and whenever FHIR's ID limit is exceeded.
    ambiguous = len(parts) > 1 and (not separator or any(separator in part for part in parts))
    if not ambiguous and re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", candidate):
        return candidate
    payload = json.dumps([prefix, separator, parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_record_identity(
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    metadata: dict[str, Any] | None,
) -> None:
    """Check keys before clinical mappings can exclude rows or hide encoder collisions."""
    identity = canonical_identity(metadata)
    if identity is None:
        return
    seen: dict[str, int] = {}
    for index, record in enumerate(records):
        try:
            encoded = resource_id({"identity": identity}, record)
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            raise ValueError(f"식별자 준비 검증 실패 record[{index}]: {exc}") from exc
        if encoded in seen:
            raise ValueError(f"식별자 준비 검증 실패: 리소스 ID가 중복됩니다 (record[{seen[encoded]}], record[{index}]).")
        seen[encoded] = index


@dataclass(frozen=True)
class RenderResult:
    resources: list[dict[str, Any]]
    errors: list[str]
    source_indices: list[int]


def render_records_detailed(
    profile: dict[str, Any],
    records: list[dict[str, Any]],
    timezone: str | None = None,
    *, metadata: dict[str, Any] | None = None, schema_path=None,
) -> RenderResult:
    """FHIR에 내부 메타데이터를 추가하지 않고 성공한 원천 행의 위치를 별도 반환합니다."""
    profile = validate_profile(profile, metadata, schema_path=schema_path)
    if (metadata or {}).get("identity_contract") is True:
        validate_record_identity(records, metadata)
    resources: list[dict[str, Any]] = []
    errors: list[str] = []
    source_indices: list[int] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        try:
            identity = resource_id(profile, record)
            if identity in seen:
                raise ValueError(f"리소스 ID가 중복됩니다: {identity}")
            resource: dict[str, Any] = {
                "resourceType": profile["target_resource"], "id": identity,
            }
            for mapping in profile["mappings"]:
                try:
                    value = apply_mapping(mapping, record, timezone)
                except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                    raise ValueError(f"mapping {mapping['id']} ({mapping['target_path']}): {exc}") from exc
                if value is not OMIT:
                    set_path(resource, mapping["target_path"], value)
            resource = prune(resource)
            # copy preserves JSON data types; unsupported objects and non-finite
            # floats must fail visibly rather than silently becoming strings.
            json.dumps(resource, allow_nan=False)
            resources.append(resource)
            source_indices.append(index)
            seen.add(identity)
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            errors.append(f"record[{index}]: {exc}")
    return RenderResult(resources, errors, source_indices)


def render_records(
    profile: dict[str, Any],
    records: list[dict[str, Any]],
    timezone: str | None = None,
    *, metadata: dict[str, Any] | None = None, schema_path=None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """동일 Profile과 동일 행에 대해 항상 같은 FHIR 리소스를 생성합니다."""

    result = render_records_detailed(profile, records, timezone, metadata=metadata, schema_path=schema_path)
    return result.resources, result.errors
