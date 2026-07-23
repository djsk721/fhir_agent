import pytest

from fhir_agent_pipeline.performance import (
    aggregate_performance_metrics,
    build_performance_metrics,
    compare_rag_performance,
)


def performance(rag_enabled: bool, accuracy: float) -> dict:
    return {
        "fhir_standard_conformance": {
            "fhir_validation_pass_rate": accuracy
        },
        "source_fidelity": {
            "source_field_accuracy": {
                "correct": int(accuracy * 10),
                "evaluated": 10,
                "accuracy": accuracy,
            }
        },
        "resource_generation_accuracy": {
            "resource_count_alignment": {
                "overall": {"alignment_rate": accuracy}
            }
        },
        "condition_procedure": {
            "precision_recall_f1": {
                "Condition": {"f1": accuracy},
                "Procedure": {"f1": accuracy},
            }
        },
        "observation": {
            "detection_f1": {"f1": accuracy},
            "value_accuracy": {"accuracy": accuracy},
            "unit_accuracy": {"accuracy": accuracy},
        },
        "reference_integrity": {
            "reference_resolution_rate": accuracy
        },
        "coding_quality": {
            "coding_accuracy": {"accuracy": accuracy}
        },
        "rag_effect": {"rag_enabled": rag_enabled},
    }


def test_build_performance_metrics_exposes_requested_metric_groups() -> None:
    validation = {
        "valid": True,
        "external_validator_used": True,
        "validator_issue_counts": {"fatal": 0, "error": 0, "warning": 1},
        "metrics": {
            "schema_valid_resource_ratio": 1.0,
            "bundle_structure_valid": True,
            "reference_resolution_ratio": 1.0,
        },
    }
    task = {
        "task_quality_pass": True,
        "vectors_enabled": True,
        "source_field_accuracy": {
            "correct": 3,
            "evaluated": 3,
            "accuracy": 1.0,
        },
        "resource_count_alignment": {
            "overall": {"expected": 1, "generated": 1, "alignment_rate": 1.0}
        },
        "resource_field_accuracy": {},
        "resource_identity_metrics": {},
        "observation_quality": {},
        "reference_accuracy": {},
        "terminology_accuracy": {},
        "generation": {},
    }

    result = build_performance_metrics(validation, task)

    assert result["overall_task_pass"]
    assert result["fhir_standard_conformance"][
        "fhir_validation_pass_rate"
    ] == 1.0
    assert "resource_count_alignment" in result[
        "resource_generation_accuracy"
    ]
    assert result["rag_effect"]["rag_ablation_improvement"] is None


def test_compare_rag_performance_calculates_macro_improvement() -> None:
    result = compare_rag_performance(
        performance(False, 0.8), performance(True, 0.9)
    )

    assert result["evaluated"]
    assert result["rag_ablation_improvement"] == pytest.approx(0.1)
    assert result["metric_improvements"]["source_field_accuracy"][
        "absolute_improvement"
    ] == pytest.approx(0.1)


def test_compare_rag_performance_rejects_wrong_modes() -> None:
    with pytest.raises(ValueError, match="rag_enabled=false"):
        compare_rag_performance(
            performance(True, 0.8), performance(True, 0.9)
        )


def test_aggregate_performance_uses_combined_denominators() -> None:
    records = [
        {
            "status": "valid",
            "metrics": {"reference_resolution_ratio": 1.0},
            "task_evaluation": {
                "task_quality_pass": True,
                "vectors_enabled": True,
                "source_field_accuracy": {
                    "correct": 9,
                    "evaluated": 10,
                },
                "resource_count_alignment": {
                    "overall": {"expected": 2, "generated": 2}
                },
                "resource_field_accuracy": {
                    "Patient": {"correct": 2, "evaluated": 2},
                    "Encounter": {"correct": 1, "evaluated": 2},
                },
                "resource_identity_metrics": {
                    "Condition": {
                        "expected": 1,
                        "generated": 1,
                        "true_positive": 1,
                        "false_positive": 0,
                        "false_negative": 0,
                    },
                    "Procedure": {},
                    "Observation": {},
                },
                "observation_quality": {},
                "reference_accuracy": {
                    "all": {"correct": 1, "evaluated": 1}
                },
                "terminology_accuracy": {
                    "coding_system_and_code": {
                        "correct": 2,
                        "evaluated": 2,
                    }
                },
                "generation": {},
            },
        },
        {
            "status": "invalid",
            "metrics": {"reference_resolution_ratio": 0.5},
            "task_evaluation": {
                "task_quality_pass": False,
                "vectors_enabled": True,
                "source_field_accuracy": {
                    "correct": 1,
                    "evaluated": 2,
                },
                "resource_count_alignment": {
                    "overall": {"expected": 3, "generated": 2}
                },
                "resource_field_accuracy": {
                    "Patient": {"correct": 1, "evaluated": 2},
                    "Encounter": {"correct": 0, "evaluated": 0},
                },
                "resource_identity_metrics": {
                    "Condition": {
                        "expected": 2,
                        "generated": 1,
                        "true_positive": 1,
                        "false_positive": 0,
                        "false_negative": 1,
                    },
                    "Procedure": {},
                    "Observation": {},
                },
                "observation_quality": {},
                "reference_accuracy": {
                    "all": {"correct": 1, "evaluated": 2}
                },
                "terminology_accuracy": {
                    "coding_system_and_code": {
                        "correct": 1,
                        "evaluated": 2,
                    }
                },
                "generation": {},
            },
        },
    ]

    result = aggregate_performance_metrics(records)

    assert result["fhir_standard_conformance"][
        "fhir_validation_pass_rate"
    ] == 0.5
    assert result["source_fidelity"]["source_field_accuracy"] == {
        "correct": 10,
        "evaluated": 12,
        "accuracy": pytest.approx(10 / 12),
    }
    assert result["condition_procedure"]["precision_recall_f1"][
        "Condition"
    ]["recall"] == pytest.approx(2 / 3)
    assert result["resource_generation_accuracy"][
        "resource_count_alignment"
    ]["overall"]["alignment_rate"] == 0.8
