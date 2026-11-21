"""환자 단위 대응 비교와 pilot 기술통계. 재표집 단위는 환자입니다."""
from __future__ import annotations

from itertools import combinations
import math
import random
from statistics import mean, variance
from typing import Any


PAIRED_METRICS = (
    "source_field_accuracy", "event_f1", "overall_pass", "event_match_coverage",
    "matched_event_field_accuracy", "expected_reference_coverage", "expected_reference_accuracy",
)


def call_cost(calls: list[dict[str, Any]]) -> dict[str, Any]:
    chats = [call for call in calls if call.get("kind") == "chat"]
    def tokens(name):
        values = [call.get(name) for call in chats]
        return sum(values) if all(isinstance(value, int) for value in values) else None
    return {
        "chat_calls": len(chats), "embedding_calls": sum(call.get("kind") == "embedding" for call in calls),
        "failed_calls": sum(call.get("success") is False for call in calls),
        "prompt_tokens": tokens("prompt_tokens"), "output_tokens": tokens("output_tokens"),
        "call_seconds": sum(call.get("elapsed_seconds", 0) for call in calls),
        "currency_cost": None,
    }


def paired_bootstrap(left: dict[str, float | None], right: dict[str, float | None],
                     *, seed: int = 42, resamples: int = 2000) -> dict[str, Any]:
    if set(left) != set(right):
        raise ValueError("대응 비교의 환자 목록이 다릅니다.")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 100:
        raise ValueError("bootstrap resamples는 100 이상의 정수여야 합니다.")
    keys = sorted(left)
    missing = [key for key in keys if left[key] is None or right[key] is None]
    if missing:
        return {"status": "not_evaluated", "patient_count": len(keys), "missing_patients": missing}
    diffs = [right[key] - left[key] for key in keys]
    if not diffs or any(not math.isfinite(value) for value in diffs):
        raise ValueError("대응 비교에는 유한한 환자별 값이 필요합니다.")
    result = {"status": "evaluated", "patient_count": len(diffs), "mean_difference": mean(diffs),
              "difference_direction": "right_minus_left", "seed": seed, "resamples": resamples,
              "confidence_interval_95": None}
    if len(diffs) < 2:
        result["confidence_interval_status"] = "insufficient_patients"
        return result
    rng = random.Random(seed)
    draws = sorted(mean(rng.choices(diffs, k=len(diffs))) for _ in range(resamples))
    result["confidence_interval_95"] = [draws[int((resamples - 1) * 0.025)], draws[int((resamples - 1) * 0.975)]]
    return result


def compare_results(left: dict[str, Any], right: dict[str, Any], *, seed=42, resamples=2000,
                    rag_ablation=False, source_rag_ablation=False) -> dict[str, Any]:
    for key in ("dataset", "cohort_sha256", "cohort_lock_sha256", "split_sha256", "independent_reference_sha256"):
        if left.get(key) != right.get(key):
            raise ValueError(f"대응 실험의 {key}가 다릅니다.")
    if not left.get("independent_reference_sha256"):
        raise ValueError("독립 참조 평가가 있는 실험만 비교할 수 있습니다.")
    if rag_ablation and source_rag_ablation:
        raise ValueError("FHIR RAG와 원천 RAG는 별도로 비교해야 합니다.")
    if rag_ablation or source_rag_ablation:
        from copy import deepcopy
        configs = [deepcopy(run["policy_config"]) for run in (left, right)]
        flag = "source_enabled" if source_rag_ablation else "enabled"
        states = [config["rag"].pop(flag, flag == "enabled") for config in configs]
        if states[0] == states[1] or configs[0] != configs[1]:
            raise ValueError(f"RAG ablation은 rag.{flag}만 다른 대응 설정이어야 합니다.")
        keys = ("model_identity", "code_sha256", "reference_profiles_sha256")
        if source_rag_ablation:
            if (not all(config["rag"].get("enabled", True) and config["rag"].get("source_contract") for config in configs)
                    or not left.get("rag_identity")
                    or any(state != bool(run.get("source_rag_identity")) for state, run in zip(states, (left, right)))):
                raise ValueError("원천 RAG 비교에는 공통 FHIR 검색과 검증된 원천 인덱스가 필요합니다.")
            keys += ("rag_identity", "fhir_constraints_identity")
        for key in keys:
            if left.get(key) != right.get(key):
                raise ValueError(f"RAG ablation의 {key}가 다릅니다.")
    results = {}
    for group in sorted(left["groups"].keys() & right["groups"].keys()):
        a, b = left["groups"][group]["patients"], right["groups"][group]["patients"]
        results[group] = {metric: paired_bootstrap(
            {key: value.get(metric) for key, value in a.items()},
            {key: value.get(metric) for key, value in b.items()}, seed=seed, resamples=resamples,
        ) for metric in PAIRED_METRICS}
    if not results:
        raise ValueError("비교 가능한 공통 군이 없습니다.")
    return {"left_policy_sha256": left["policy_sha256"], "right_policy_sha256": right["policy_sha256"],
            "dataset": left["dataset"], "rag_ablation": rag_ablation,
            "source_rag_ablation": source_rag_ablation, "groups": results}


def within_run_comparisons(groups: dict[str, Any], *, seed=42, resamples=2000) -> dict[str, Any]:
    comparisons = {}
    for left, right in combinations(groups, 2):
        a, b = groups[left]["patients"], groups[right]["patients"]
        comparisons[f"{left}_vs_{right}"] = {metric: paired_bootstrap(
            {key: value.get(metric) for key, value in a.items()},
            {key: value.get(metric) for key, value in b.items()}, seed=seed, resamples=resamples,
        ) for metric in PAIRED_METRICS}
    return comparisons


def pilot_summary(groups: dict[str, Any], *, target_half_width: float | None = None) -> dict[str, Any]:
    if target_half_width is not None and not 0 < target_half_width < 1:
        raise ValueError("target_half_width는 0~1 사이여야 합니다.")
    summary = {}
    for name, group in groups.items():
        patients = list(group["patients"].values())
        counts = [patient["source_record_count"] for patient in patients]
        summary[name] = {
            "patients": len(patients), "source_records": sum(counts),
            "records_per_patient": {"min": min(counts), "max": max(counts), "mean": mean(counts)},
            "failed_patients": sum(patient["validation"]["valid"] is False for patient in patients),
            "fidelity_evaluated_patients": sum(patient["source_field_accuracy"] is not None for patient in patients),
        }
    paired = {}
    for left, right in combinations(groups, 2):
        a, b = groups[left]["patients"], groups[right]["patients"]
        diffs = [b[key]["source_field_accuracy"] - a[key]["source_field_accuracy"] for key in a
                 if b[key]["source_field_accuracy"] is not None and a[key]["source_field_accuracy"] is not None]
        sample_variance = variance(diffs) if len(diffs) > 1 else None
        # Pilot-based normal approximation for CI precision, not hypothesis-test power.
        estimated = None
        if target_half_width and sample_variance is not None and sample_variance > 0:
            estimated = math.ceil(1.96 ** 2 * sample_variance / target_half_width ** 2)
        paired[f"{left}_vs_{right}"] = {
            "evaluated_patients": len(diffs), "paired_difference_variance": sample_variance,
            "target_half_width": target_half_width, "estimated_patients_for_mean_precision": estimated,
            "method": "normal_approximation_patient_paired_difference",
        }
    return {"groups": summary, "sample_size_planning": paired}
