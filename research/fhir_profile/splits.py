"""Patient-level, reproducible partitions for development and held-out evaluation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any


def _roles(dataset: str) -> tuple[str, ...]:
    if not isinstance(dataset, str):
        raise ValueError("dataset은 문자열이어야 합니다.")
    normalized = dataset.lower().replace("-", "_")
    if normalized.startswith("mimic"):
        return ("development", "validation", "test")
    if normalized == "synthea":
        return ("calibration", "test")
    raise ValueError("dataset은 mimic_iv 또는 synthea여야 합니다.")


def _checked_keys(patient_keys: Iterable[Any]) -> list[str | int]:
    if isinstance(patient_keys, (str, bytes, Mapping)):
        raise ValueError("환자 키는 문자열 자체가 아닌 목록이어야 합니다.")
    keys = list(patient_keys)
    seen: set[str] = set()
    for key in keys:
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise ValueError("환자 키는 문자열 또는 정수여야 합니다.")
        if isinstance(key, str) and not key.strip():
            raise ValueError("환자 키는 비어 있을 수 없습니다.")
        # CSV and database identifiers must not leak across splits merely because
        # an integer key was serialized to a string in one input file.
        identity = str(key)
        if identity in seen:
            raise ValueError(f"중복 환자 키: {key!r}")
        seen.add(identity)
    return keys


def _digest(payload: Mapping[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_patient_split(
    patient_keys: Iterable[str | int],
    dataset: str,
    seed: int = 42,
    fractions: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Create disjoint partitions; preserve original key types and reject duplicates.

    Input ordering does not affect the result. Synthea calibration patients may
    support profile generation/correction; test patients remain held out.
    """
    roles = _roles(dataset)
    weights = dict(fractions if fractions is not None else (
        {"development": 0.6, "validation": 0.2, "test": 0.2}
        if len(roles) == 3 else {"calibration": 0.2, "test": 0.8}
    ))
    if set(weights) != set(roles):
        raise ValueError(f"분할 역할은 {roles}이어야 합니다.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("분할 비율은 유한한 0 이상의 숫자여야 합니다.")
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError("분할 비율 합은 1이어야 합니다.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed는 정수여야 합니다.")
    keys = _checked_keys(patient_keys)
    if not keys:
        raise ValueError("환자 목록은 비어 있을 수 없습니다.")
    # Hash ranking avoids depending on a Python-version-specific PRNG shuffle.
    keys.sort(key=lambda key: hashlib.sha256(
        json.dumps([seed, key], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest())
    raw_counts = {role: len(keys) * weights[role] for role in roles}
    counts = {role: math.floor(raw_counts[role]) for role in roles}
    remaining = len(keys) - sum(counts.values())
    ranked = sorted(roles, key=lambda role: -(raw_counts[role] - counts[role]))
    for role in ranked[:remaining]:
        counts[role] += 1
    partitions: dict[str, list[str | int]] = {}
    start = 0
    for role in roles:
        partitions[role] = keys[start:start + counts[role]]
        start += counts[role]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "dataset": dataset,
        "seed": seed,
        "fractions": weights,
        "roles": partitions,
        "patient_count": len(keys),
        "assignment_unit": "patient",
        "assignment_method": "sha256-ranking-v1",
    }
    payload["manifest_sha256"] = _digest(payload)
    return payload


def validate_patient_split(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reject overlapping, corrupted, or incomplete partition manifests."""
    if payload.get("schema_version") != 1:
        raise ValueError("지원하지 않는 split schema_version입니다.")
    dataset = payload.get("dataset")
    if not isinstance(dataset, str):
        raise ValueError("분할 dataset이 필요합니다.")
    roles = _roles(dataset)
    partitions = payload.get("roles")
    if not isinstance(partitions, dict) or set(partitions) != set(roles):
        raise ValueError(f"분할 역할은 {roles}이어야 합니다.")
    if any(not isinstance(partitions[role], list) for role in roles):
        raise ValueError("각 분할은 환자 키 배열이어야 합니다.")
    all_keys = _checked_keys(key for role in roles for key in partitions[role])
    if isinstance(payload.get("patient_count"), bool) or payload.get("patient_count") != len(all_keys):
        raise ValueError("patient_count가 실제 환자 수와 다릅니다.")
    if payload.get("assignment_unit") != "patient":
        raise ValueError("분할 단위는 patient여야 합니다.")
    method = payload.get("assignment_method")
    if method == "fixed-roles-v1":
        expected = create_fixed_patient_split(partitions, dataset)
        if expected != payload:
            raise ValueError("고정 환자 분할의 내용 또는 해시가 일치하지 않습니다.")
    elif method == "sha256-ranking-v1":
        if not isinstance(payload.get("fractions"), dict):
            raise ValueError("분할 fractions 객체가 필요합니다.")
        expected = create_patient_split(
            all_keys, dataset=dataset, seed=payload.get("seed"), fractions=payload.get("fractions")
        )
        if expected["roles"] != partitions:
            raise ValueError("환자 분할이 seed와 비율로 재현되지 않습니다.")
    else:
        raise ValueError("지원하지 않는 환자 분할 방법입니다.")
    if payload.get("manifest_sha256") != _digest(payload):
        raise ValueError("분할 manifest 해시가 일치하지 않습니다.")
    return copy.deepcopy(dict(payload))


def create_fixed_patient_split(partitions: Mapping[str, list[str | int]], dataset: str) -> dict[str, Any]:
    """Freeze explicitly assigned patient IDs; input ordering does not change the manifest."""
    roles = _roles(dataset)
    if not isinstance(partitions, Mapping) or set(partitions) != set(roles):
        raise ValueError(f"고정 환자 파일의 역할은 {roles}이어야 합니다.")
    if any(not isinstance(partitions[role], list) for role in roles):
        raise ValueError("각 분할은 환자 키 배열이어야 합니다.")
    keys = _checked_keys(key for role in roles for key in partitions[role])
    if not partitions["test"] or not any(partitions[role] for role in roles if role != "test"):
        raise ValueError("평가 환자와 별도의 준비 환자가 모두 필요합니다.")
    payload = {
        "schema_version": 1,
        "dataset": dataset,
        "roles": {role: sorted(partitions[role], key=str) for role in roles},
        "patient_count": len(keys),
        "assignment_unit": "patient",
        "assignment_method": "fixed-roles-v1",
    }
    return {**payload, "manifest_sha256": _digest(payload)}


def patient_keys_for(split: Mapping[str, Any], role: str) -> list[str | int]:
    validated = validate_patient_split(split)
    if role not in validated["roles"]:
        raise ValueError(f"알 수 없는 분할 역할: {role}")
    return list(validated["roles"][role])
