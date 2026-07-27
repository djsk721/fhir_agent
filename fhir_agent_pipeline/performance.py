from __future__ import annotations

from typing import Any


def _combine_generation_metrics(
    generation: dict[str, dict[str, Any]],
) -> dict[str, int | float]:
    batches = sum(int(item.get("batches", 0)) for item in generation.values())
    first_pass = sum(
        int(item.get("first_pass_valid_batches", 0))
        for item in generation.values()
    )
    repaired = sum(
        int(item.get("repaired_batches", 0))
        for item in generation.values()
    )
    repair_attempts = sum(
        int(item.get("repair_attempts", 0))
        for item in generation.values()
    )
    initially_invalid = batches - first_pass
    return {
        "batches": batches,
        "first_pass_validity_rate": (
            1.0 if not batches else first_pass / batches
        ),
        "repair_success_rate": (
            1.0 if not initially_invalid else repaired / initially_invalid
        ),
        "repair_attempts": repair_attempts,
        "average_repair_attempts": (
            0.0 if not batches else repair_attempts / batches
        ),
    }


def build_performance_metrics(
    validation: dict[str, Any],
    task_evaluation: dict[str, Any],
) -> dict[str, Any]:
    """요청한 대표 성능평가 지표를 한 구조로 정리합니다."""

    field_accuracy = task_evaluation.get("resource_field_accuracy", {})
    identity = task_evaluation.get("resource_identity_metrics", {})
    observation = task_evaluation.get("observation_quality", {})
    terminology = task_evaluation.get("terminology_accuracy", {})
    references = task_evaluation.get("reference_accuracy", {})
    generation = task_evaluation.get("generation", {})
    external_errors = sum(
        int(validation.get("validator_issue_counts", {}).get(name, 0))
        for name in ("fatal", "error")
    )
    fhir_pass = bool(validation.get("valid")) and (
        not validation.get("external_validator_used") or external_errors == 0
    )
    validation_metrics = validation.get("metrics", {})
    task_pass = bool(task_evaluation.get("task_quality_pass"))

    return {
        "overall_task_pass": fhir_pass and task_pass,
        "fhir_standard_conformance": {
            "fhir_validation_pass": fhir_pass,
            "fhir_validation_pass_rate": 1.0 if fhir_pass else 0.0,
            "external_validator_used": bool(
                validation.get("external_validator_used")
            ),
            "validator_issue_counts": validation.get(
                "validator_issue_counts", {}
            ),
            "json_schema_pass_rate": validation_metrics.get(
                "schema_valid_resource_ratio"
            ),
            "bundle_structure_pass": validation_metrics.get(
                "bundle_structure_valid"
            ),
        },
        "source_fidelity": {
            "source_field_accuracy": task_evaluation.get(
                "source_field_accuracy", {}
            ),
        },
        "resource_generation_accuracy": {
            "resource_count_alignment": task_evaluation.get(
                "resource_count_alignment", {}
            ),
        },
        "patient_encounter": {
            "field_level_accuracy": {
                name: field_accuracy.get(name, {})
                for name in ("Patient", "Encounter")
            },
        },
        "condition_procedure": {
            "precision_recall_f1": {
                name: identity.get(name, {})
                for name in ("Condition", "Procedure")
            },
        },
        "observation": {
            "detection_f1": observation.get("detection", {}),
            "value_accuracy": observation.get("value_accuracy", {}),
            "unit_accuracy": observation.get("unit_accuracy", {}),
            "time_accuracy": observation.get("time_accuracy", {}),
        },
        "reference_integrity": {
            "reference_resolution_rate": validation_metrics.get(
                "reference_resolution_ratio"
            ),
            "source_reference_accuracy": references,
        },
        "coding_quality": {
            "coding_accuracy": terminology.get(
                "coding_system_and_code", {}
            ),
            "source_text_accuracy": terminology.get("source_text", {}),
            "details": terminology,
        },
        "self_correction": _combine_generation_metrics(generation),
        "rag_effect": {
            "rag_enabled": bool(task_evaluation.get("vectors_enabled")),
            "evaluated": False,
            "rag_ablation_improvement": None,
            "comparison_status": (
                "paired LLM-only and LLM+RAG results are required"
            ),
        },
    }


def _aggregate_scores(
    evaluations: list[dict[str, Any]],
    *path: str,
) -> dict[str, int | float]:
    correct = 0
    evaluated = 0
    for evaluation in evaluations:
        value: Any = evaluation
        for part in path:
            value = value.get(part, {}) if isinstance(value, dict) else {}
        if isinstance(value, dict):
            correct += int(value.get("correct", 0))
            evaluated += int(value.get("evaluated", 0))
    return {
        "correct": correct,
        "evaluated": evaluated,
        "accuracy": 1.0 if not evaluated else correct / evaluated,
    }


def _aggregate_f1(
    evaluations: list[dict[str, Any]],
    resource_type: str,
) -> dict[str, int | float]:
    values = [
        item.get("resource_identity_metrics", {}).get(resource_type, {})
        for item in evaluations
    ]
    expected = sum(int(item.get("expected", 0)) for item in values)
    generated = sum(int(item.get("generated", 0)) for item in values)
    true_positive = sum(
        int(item.get("true_positive", 0)) for item in values
    )
    false_positive = sum(
        int(item.get("false_positive", 0)) for item in values
    )
    false_negative = sum(
        int(item.get("false_negative", 0)) for item in values
    )
    precision = (
        1.0
        if not generated
        else true_positive / (true_positive + false_positive)
    )
    recall = (
        1.0
        if not expected
        else true_positive / (true_positive + false_negative)
    )
    return {
        "expected": expected,
        "generated": generated,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": (
            0.0
            if precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        ),
    }


def aggregate_performance_metrics(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """환자별 분자·분모를 합산해 cohort 대표 지표를 계산합니다."""

    evaluated_records = [
        item for item in records if item.get("status") in {"valid", "invalid"}
    ]
    evaluations = [
        item["task_evaluation"]
        for item in evaluated_records
        if isinstance(item.get("task_evaluation"), dict)
    ]
    strict_records = [
        item
        for item in evaluated_records
        if isinstance(item.get("task_evaluation"), dict)
    ]
    validation_count = len(evaluated_records)
    valid_count = sum(
        item.get("status") == "valid" for item in evaluated_records
    )
    count_expected = sum(
        int(
            item.get("resource_count_alignment", {})
            .get("overall", {})
            .get("expected", 0)
        )
        for item in evaluations
    )
    count_generated = sum(
        int(
            item.get("resource_count_alignment", {})
            .get("overall", {})
            .get("generated", 0)
        )
        for item in evaluations
    )
    count_denominator = max(count_expected, count_generated)
    reference_rates = [
        item.get("metrics", {}).get("reference_resolution_ratio")
        for item in evaluated_records
    ]
    reference_rates = [
        float(value)
        for value in reference_rates
        if isinstance(value, (int, float))
    ]
    generation = {
        f"{patient_index}:{resource_type}": metrics
        for patient_index, evaluation in enumerate(evaluations)
        for resource_type, metrics in evaluation.get(
            "generation", {}
        ).items()
    }
    vector_modes = {
        bool(item.get("vectors_enabled")) for item in evaluations
    }

    return {
        "overall_task_pass_rate": (
            None
            if not evaluations
            else sum(
                bool(item.get("task_quality_pass"))
                for item in evaluations
            )
            / len(evaluations)
        ),
        "strict_end_to_end_pass_rate": (
            None
            if not strict_records
            else sum(
                item.get("status") == "valid"
                and bool(
                    item["task_evaluation"].get(
                        "task_quality_pass"
                    )
                )
                for item in strict_records
            )
            / len(strict_records)
        ),
        "fhir_standard_conformance": {
            "evaluated_bundles": validation_count,
            "passed_bundles": valid_count,
            "fhir_validation_pass_rate": (
                None
                if not validation_count
                else valid_count / validation_count
            ),
        },
        "source_fidelity": {
            "source_field_accuracy": _aggregate_scores(
                evaluations, "source_field_accuracy"
            ),
        },
        "resource_generation_accuracy": {
            "resource_count_alignment": {
                "overall": {
                    "expected": count_expected,
                    "generated": count_generated,
                    "difference": count_generated - count_expected,
                    "alignment_rate": (
                        1.0
                        if not count_denominator
                        else 1.0
                        - abs(count_generated - count_expected)
                        / count_denominator
                    ),
                }
            },
        },
        "patient_encounter": {
            "field_level_accuracy": {
                name: _aggregate_scores(
                    evaluations, "resource_field_accuracy", name
                )
                for name in ("Patient", "Encounter")
            },
        },
        "condition_procedure": {
            "precision_recall_f1": {
                name: _aggregate_f1(evaluations, name)
                for name in ("Condition", "Procedure")
            },
        },
        "observation": {
            "detection_f1": _aggregate_f1(
                evaluations, "Observation"
            ),
            "value_accuracy": _aggregate_scores(
                evaluations, "observation_quality", "value_accuracy"
            ),
            "unit_accuracy": _aggregate_scores(
                evaluations, "observation_quality", "unit_accuracy"
            ),
            "time_accuracy": _aggregate_scores(
                evaluations, "observation_quality", "time_accuracy"
            ),
        },
        "reference_integrity": {
            "reference_resolution_rate": (
                None
                if not reference_rates
                else sum(reference_rates) / len(reference_rates)
            ),
            "source_reference_accuracy": _aggregate_scores(
                evaluations, "reference_accuracy", "all"
            ),
        },
        "coding_quality": {
            "coding_accuracy": _aggregate_scores(
                evaluations,
                "terminology_accuracy",
                "coding_system_and_code",
            ),
            "source_text_accuracy": _aggregate_scores(
                evaluations,
                "terminology_accuracy",
                "source_text",
            ),
        },
        "self_correction": _combine_generation_metrics(generation),
        "rag_effect": {
            "rag_enabled": (
                next(iter(vector_modes)) if len(vector_modes) == 1 else None
            ),
            "rag_ablation_improvement": None,
            "comparison_status": (
                "use compare-rag with paired cohort manifests"
            ),
        },
    }


def compare_rag_performance(
    baseline: dict[str, Any],
    rag: dict[str, Any],
) -> dict[str, Any]:
    """동일 cohort의 LLM-only와 LLM+RAG 대표 지표 차이를 계산합니다."""

    baseline_mode = baseline.get("rag_effect", {}).get("rag_enabled")
    rag_mode = rag.get("rag_effect", {}).get("rag_enabled")
    if baseline_mode is not False or rag_mode is not True:
        raise ValueError(
            "RAG 비교에는 rag_enabled=false baseline과 "
            "rag_enabled=true 결과가 순서대로 필요합니다."
        )

    paths = {
        "fhir_validation_pass_rate": (
            "fhir_standard_conformance",
            "fhir_validation_pass_rate",
        ),
        "source_field_accuracy": (
            "source_fidelity",
            "source_field_accuracy",
            "accuracy",
        ),
        "resource_count_alignment": (
            "resource_generation_accuracy",
            "resource_count_alignment",
            "overall",
            "alignment_rate",
        ),
        "patient_field_accuracy": (
            "patient_encounter",
            "field_level_accuracy",
            "Patient",
            "accuracy",
        ),
        "encounter_field_accuracy": (
            "patient_encounter",
            "field_level_accuracy",
            "Encounter",
            "accuracy",
        ),
        "condition_f1": (
            "condition_procedure",
            "precision_recall_f1",
            "Condition",
            "f1",
        ),
        "procedure_f1": (
            "condition_procedure",
            "precision_recall_f1",
            "Procedure",
            "f1",
        ),
        "observation_detection_f1": (
            "observation",
            "detection_f1",
            "f1",
        ),
        "observation_value_accuracy": (
            "observation",
            "value_accuracy",
            "accuracy",
        ),
        "observation_unit_accuracy": (
            "observation",
            "unit_accuracy",
            "accuracy",
        ),
        "reference_resolution_rate": (
            "reference_integrity",
            "reference_resolution_rate",
        ),
        "coding_accuracy": (
            "coding_quality",
            "coding_accuracy",
            "accuracy",
        ),
        "repair_success_rate": (
            "self_correction",
            "repair_success_rate",
        ),
    }

    def read(value: dict[str, Any], path: tuple[str, ...]) -> float | None:
        current: Any = value
        for part in path:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return float(current) if isinstance(current, (int, float)) else None

    improvements: dict[str, dict[str, float]] = {}
    quality_deltas: list[float] = []
    for name, path in paths.items():
        before = read(baseline, path)
        after = read(rag, path)
        if before is None or after is None:
            continue
        delta = after - before
        improvements[name] = {
            "llm_only": before,
            "llm_with_rag": after,
            "absolute_improvement": delta,
        }
        if name != "repair_success_rate":
            quality_deltas.append(delta)

    return {
        "evaluated": bool(improvements),
        "rag_ablation_improvement": (
            None
            if not quality_deltas
            else sum(quality_deltas) / len(quality_deltas)
        ),
        "metric_improvements": improvements,
        "note": "absolute improvement is LLM+RAG minus LLM-only",
    }
