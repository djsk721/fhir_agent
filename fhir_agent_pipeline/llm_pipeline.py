from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import Settings
from .fhir_models import build_bundle
from .evaluation import evaluate_task_output
from .fhir_schema import get_fhir_schema
from .llm_query_service import LlmClinicalQueryService, LlmQueryTrace
from .performance import build_performance_metrics
from .resource_registry import selected_adapters
from .resource_renderer import DeterministicResourceRenderer
from .source_profile import SourceProfile
from .sql_agent import SqlGenerationError
from .validation import ValidationReport, validate_bundle_detailed


@dataclass(frozen=True)
class SqlToFhirPipelineResult:
    fhir_payload: dict[str, Any]
    generated_resources: list[dict[str, Any]]
    verification_errors: list[str]
    validation_report: ValidationReport | None
    query_traces: list[LlmQueryTrace]
    source_row_counts: dict[str, int]
    llm_calls_used: int
    rendering_metrics: dict[str, dict[str, int | float]]
    task_evaluation: dict[str, Any]

    def trace_dict(self) -> dict[str, Any]:
        sql_metrics = _sql_metrics(self.query_traces)
        return {
            "mode": "llm-sql-and-deterministic-fhir",
            "llm_calls_used": self.llm_calls_used,
            "source_row_counts": self.source_row_counts,
            "rendering_metrics": self.rendering_metrics,
            "task_evaluation": self.task_evaluation,
            "performance_evaluation": (
                self.validation_report.metrics.get(
                    "performance_evaluation", {}
                )
                if self.validation_report
                else {}
            ),
            "sql_generation_metrics": sql_metrics,
            "rag_configuration": {
                "vectors_enabled": self.task_evaluation.get(
                    "vectors_enabled", True
                ),
            },
            "queries": [asdict(item) for item in self.query_traces],
        }


class PipelineExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        resource_type: str | None,
        cause: Exception,
        query_traces: list[LlmQueryTrace],
        source_row_counts: dict[str, int],
        rendering_metrics: dict[str, dict[str, int | float]],
    ) -> None:
        super().__init__(message)
        self.resource_type = resource_type
        self.cause = cause
        self.query_traces = list(query_traces)
        self.source_row_counts = dict(source_row_counts)
        self.rendering_metrics = dict(rendering_metrics)

    def trace_dict(self) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "resource_type": self.resource_type,
            "error_type": type(self.cause).__name__,
            "message": str(self.cause),
        }
        if isinstance(self.cause, SqlGenerationError):
            failure["sql_attempts"] = [
                asdict(item) for item in self.cause.attempts
            ]
        return {
            "mode": "llm-sql-and-deterministic-fhir",
            "status": "failed",
            "failure": failure,
            "source_row_counts": self.source_row_counts,
            "rendering_metrics": self.rendering_metrics,
            "sql_generation_metrics": _sql_metrics(self.query_traces),
            "queries": [asdict(item) for item in self.query_traces],
        }


class LlmSqlToFhirPipeline:
    """LLM SQL 조회와 결정적 FHIR Renderer를 순서대로 조정합니다."""

    def __init__(
        self,
        settings: Settings,
        query_service: LlmClinicalQueryService | None = None,
        renderer: DeterministicResourceRenderer | None = None,
    ) -> None:
        self.settings = settings
        self.profile = SourceProfile.load(settings.source_profile_path)
        schema = get_fhir_schema(settings.fhir_schema_path)
        self.query_service = query_service or LlmClinicalQueryService(
            settings, profile=self.profile
        )
        if renderer is None:
            self.renderer = DeterministicResourceRenderer(
                settings, schema
            )
        else:
            self.renderer = renderer

    def run(
        self,
        patient_key: int | str,
        encounter_key: int | str | None = None,
    ) -> SqlToFhirPipelineResult:
        generated: list[dict[str, Any]] = []
        traces: list[LlmQueryTrace] = []
        source_counts: dict[str, int] = {}
        rendering_metrics: dict[str, dict[str, int | float]] = {}
        source_rows: dict[str, list[dict[str, Any]]] = {}
        generated_by_type: dict[str, list[dict[str, Any]]] = {}
        encounter_keys: list[int | str] = []
        total_calls = 0
        current_resource_type: str | None = None

        try:
            for adapter in selected_adapters(self.settings.fhir_output_resources):
                current_resource_type = adapter.resource_type
                queried = self.query_service.query(
                    adapter.resource_type,
                    patient_key,
                    encounter_keys,
                    encounter_key,
                )
                traces.append(queried.trace)
                source_counts[adapter.resource_type] = len(queried.rows)
                source_rows[adapter.resource_type] = queried.rows
                total_calls += queried.trace.llm_calls_used
                if adapter.resource_type == "Encounter":
                    encounter_keys = self.query_service.encounter_ids(queried.rows)

                render_result = self.renderer.generate(
                    adapter.resource_type,
                    queried.rows,
                    patient_key,
                    encounter_keys,
                )
                rendering_metrics[adapter.resource_type] = (
                    render_result.metrics()
                )
                generated_by_type[adapter.resource_type] = (
                    render_result.resources
                )
                generated.extend(render_result.resources)
        except Exception as exc:
            raise PipelineExecutionError(
                f"{current_resource_type or 'Pipeline'} 처리 실패: {exc}",
                resource_type=current_resource_type,
                cause=exc,
                query_traces=traces,
                source_row_counts=source_counts,
                rendering_metrics=rendering_metrics,
            ) from exc
        finally:
            self.query_service.close()

        bundle = build_bundle(
            generated, namespace=self.profile.bundle_namespace
        )
        report = validate_bundle_detailed(bundle, self.settings)
        task_evaluation = evaluate_task_output(
            source_rows,
            generated_by_type,
            self.settings.fhir_default_timezone,
        )
        task_evaluation["vectors_enabled"] = self.settings.vectors_enabled
        task_evaluation["rendering"] = rendering_metrics
        task_evaluation["sql"] = _sql_metrics(traces)
        report_dict = report.as_dict()
        performance = build_performance_metrics(
            report_dict, task_evaluation
        )
        report = replace(
            report,
            metrics={
                **report.metrics,
                "task_evaluation": task_evaluation,
                "performance_evaluation": performance,
            },
        )
        return SqlToFhirPipelineResult(
            fhir_payload=bundle,
            generated_resources=generated,
            verification_errors=list(report.errors),
            validation_report=report,
            query_traces=traces,
            source_row_counts=source_counts,
            llm_calls_used=total_calls,
            rendering_metrics=rendering_metrics,
            task_evaluation=task_evaluation,
        )


def _sql_metrics(traces: list[LlmQueryTrace]) -> dict[str, int | float]:
    categories = [
        category for trace in traces for category in trace.failure_categories
    ]
    safety_rejections = categories.count("safety_policy")
    database_failures = categories.count("database_execution")
    result_validation_failures = sum(
        category.startswith("result_") for category in categories
    )
    safety_checks = len(traces) + safety_rejections + database_failures
    return {
        "queries": len(traces),
        "first_pass_successes": sum(item.llm_calls_used == 1 for item in traces),
        "first_pass_success_rate": (
            1.0
            if not traces
            else sum(item.llm_calls_used == 1 for item in traces) / len(traces)
        ),
        "repair_attempts": sum(max(0, item.llm_calls_used - 1) for item in traces),
        "response_format_failures": categories.count("response_format"),
        "safety_policy_rejections": safety_rejections,
        "database_execution_failures": database_failures,
        "result_validation_failures": result_validation_failures,
        "result_empty_required_failures": categories.count(
            "result_empty_required"
        ),
        "result_required_column_failures": categories.count(
            "result_required_columns"
        ),
        "result_scope_failures": sum(
            category in {"result_patient_scope", "result_encounter_scope"}
            for category in categories
        ),
        "rag_requested_queries": sum(item.rag_requested for item in traces),
        "rag_used_queries": sum(item.rag_used for item in traces),
        "retrieved_documents": sum(
            item.retrieved_document_count for item in traces
        ),
        "sql_safety_pass_rate": (
            1.0
            if not safety_checks
            else (safety_checks - safety_rejections) / safety_checks
        ),
    }
