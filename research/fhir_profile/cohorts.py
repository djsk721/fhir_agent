"""Content locks for previously selected preparation and evaluation cohorts."""
from __future__ import annotations

from typing import Any

from .artifacts import fingerprint
from .experiment import verify_cohort
from .profiles.schema import validate_profile


def _role_data(cohort: dict[str, Any]) -> dict[str, Any]:
    return {"role": cohort["role"], "patient_count": len(cohort["patient_keys"]),
            "cohort_sha256": cohort["cohort_sha256"]}


def _reference_hash(cohort, reference_resources):
    if reference_resources is None:
        return None
    patients = [str(item.get("id")) for item in reference_resources if item.get("resourceType") == "Patient"]
    if sorted(patients) != sorted(map(str, cohort["patient_keys"])):
        raise ValueError("참조 FHIR의 Patient가 고정 평가 환자와 일치하지 않습니다.")
    return fingerprint(reference_resources)


def _mapping_hash(cohort, reference_profiles):
    if reference_profiles is None:
        return None
    if not isinstance(reference_profiles, dict) or not reference_profiles or set(reference_profiles) - set(cohort["resources"]):
        raise ValueError("독립 매핑 정답은 연구 범위의 resourceType별 객체여야 합니다.")
    for kind, profile in reference_profiles.items():
        batch = cohort["resources"][kind]
        validate_profile(profile, batch["metadata"])
        if profile["target_resource"] != kind or profile["source"] != {
            "dataset": cohort["dataset"], "table": batch["source_resource"],
        }:
            raise ValueError(f"{kind} 독립 매핑 정답의 대상 리소스·원천 범위가 다릅니다.")
    return fingerprint(reference_profiles)


def create_cohort_lock(preparation, evaluation, *, reference_resources=None, reference_profiles=None):
    """Pin membership, rows and independent evaluation inputs without copying patient IDs."""
    verify_cohort(preparation)
    verify_cohort(evaluation)
    dataset = preparation["dataset"]
    roles = (preparation["role"], evaluation["role"])
    allowed = {
        "mimic_iv": {("development", "validation"), ("development", "test"), ("validation", "test")},
        "synthea": {("calibration", "test")},
    }
    if dataset != evaluation["dataset"] or roles not in allowed.get(dataset, set()):
        raise ValueError("데이터셋에 맞는 서로 다른 준비·평가 역할이 필요합니다.")
    split_sha = preparation["split"]["manifest_sha256"]
    if split_sha != evaluation["split"]["manifest_sha256"]:
        raise ValueError("준비와 평가의 환자 분할이 다릅니다.")
    if preparation["extract_spec_sha256"] != evaluation["extract_spec_sha256"]:
        raise ValueError("준비와 평가의 추출 명세가 다릅니다.")
    if set(map(str, preparation["patient_keys"])) & set(map(str, evaluation["patient_keys"])):
        raise ValueError("준비와 평가의 환자가 중복됩니다.")
    payload = {
        "format_version": 1,
        "dataset": dataset,
        "split_sha256": split_sha,
        "extract_spec_sha256": evaluation["extract_spec_sha256"],
        "preparation": _role_data(preparation),
        "evaluation": _role_data(evaluation),
        "independent_reference_sha256": _reference_hash(evaluation, reference_resources),
        "reference_profiles_sha256": _mapping_hash(evaluation, reference_profiles),
    }
    return {**payload, "cohort_lock_sha256": fingerprint(payload)}


def verify_locked_cohort(lock, cohort, stage, *, reference_resources=None, reference_profiles=None, profiles=None):
    unsigned = {key: value for key, value in lock.items() if key != "cohort_lock_sha256"}
    if lock.get("format_version") != 1 or fingerprint(unsigned) != lock.get("cohort_lock_sha256"):
        raise ValueError("고정 코호트 잠금의 버전 또는 해시가 일치하지 않습니다.")
    verify_cohort(cohort)
    if stage not in ("preparation", "evaluation"):
        raise ValueError("코호트 검증 단계는 preparation 또는 evaluation이어야 합니다.")
    if (cohort["dataset"] != lock["dataset"]
            or cohort["split"]["manifest_sha256"] != lock["split_sha256"]
            or cohort["extract_spec_sha256"] != lock["extract_spec_sha256"]
            or _role_data(cohort) != lock[stage]):
        raise ValueError(f"고정 코호트의 {stage} 원천 자료 또는 분할이 일치하지 않습니다.")
    if stage == "evaluation":
        if _reference_hash(cohort, reference_resources) != lock["independent_reference_sha256"]:
            raise ValueError("고정 코호트의 독립 참조 FHIR가 일치하지 않습니다.")
        if _mapping_hash(cohort, reference_profiles) != lock["reference_profiles_sha256"]:
            raise ValueError("고정 코호트의 독립 매핑 정답이 일치하지 않습니다.")
        if profiles is not None and profiles.get("preparation_cohort_sha256") != lock["preparation"]["cohort_sha256"]:
            raise ValueError("고정 코호트의 준비 원천 자료로 생성한 Profile이 아닙니다.")
