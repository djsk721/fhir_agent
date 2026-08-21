from __future__ import annotations

from dataclasses import dataclass, replace
import gzip
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator
from urllib.parse import quote, unquote

from .config import Settings
from .json_utils import dump_json
from .llm_pipeline import LlmSqlToFhirPipeline, PipelineExecutionError
from .performance import (
    aggregate_performance_metrics,
    build_performance_metrics,
)
from .source_profile import SourceProfile
from .validation import validate_bundle_detailed


@dataclass(frozen=True)
class ExportRequest:
    output_directory: Path
    patient_keys: tuple[int | str, ...] = ()
    limit: int | None = None
    all_cohort_patients: bool = False
    resume: bool = False

    def validate(self) -> None:
        selectors = sum(
            (
                bool(self.patient_keys),
                self.limit is not None,
                self.all_cohort_patients,
            )
        )
        if selectors != 1:
            raise ValueError(
                "patient_keys, limit, all_cohort_patients 중 하나만 지정해야 합니다."
            )
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")


def resolve_cohort_patient_file(settings: Settings) -> Path:
    """선택한 source profile의 패턴으로 cohort Patient 파일을 찾습니다."""

    profile = SourceProfile.load(settings.source_profile_path)
    candidates: list[Path] = []
    for pattern in profile.cohort.patient_file_patterns:
        candidates.extend(settings.source_data_path.glob(pattern))
    available = sorted({path.resolve() for path in candidates if path.is_file()})
    if not available:
        raise FileNotFoundError(
            f"{profile.name} cohort Patient 파일을 찾을 수 없습니다: "
            f"{settings.source_data_path}"
        )
    def natural_key(path: Path) -> tuple[tuple[int, int | str], ...]:
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in re.split(r"(\d+)", str(path))
            if part
        )

    return max(available, key=natural_key)


def _cohort_patient_key(
    resource: dict[str, Any],
    path: Path,
    line_number: int,
    *,
    identifier_system: str | None = None,
    key_type: str = "integer",
) -> int | str:
    if resource.get("resourceType") != "Patient":
        raise ValueError(f"{path.name}:{line_number}은 Patient 리소스가 아닙니다.")
    identifiers = resource.get("identifier", [])
    values: set[int | str] = set()
    for identifier in identifiers if isinstance(identifiers, list) else []:
        if not isinstance(identifier, dict):
            continue
        raw_value = str(identifier.get("value", "")).strip()
        if not raw_value:
            continue
        if key_type == "integer":
            try:
                normalized_value: int | str = int(raw_value)
            except ValueError:
                continue
        else:
            normalized_value = raw_value
        if not identifier_system or identifier.get("system") == identifier_system:
            values.add(normalized_value)
    if len(values) != 1:
        raise ValueError(
            f"{path.name}:{line_number}에서 DB 환자 키 identifier를 "
            f"하나로 결정할 수 없습니다: {sorted(str(value) for value in values)}"
        )
    return values.pop()


def iter_cohort_patient_ids(
    settings: Settings, limit: int | None
) -> Iterator[int | str]:
    """Source profile의 Patient NDJSON에서 DB 환자 키를 읽습니다."""

    if limit is not None and limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다.")
    profile = SourceProfile.load(settings.source_profile_path)
    if profile.cohort.provider != "fhir-patient-ndjson":
        raise ValueError(
            f"{profile.name} profile은 --limit/--all cohort provider를 "
            f"지원하지 않습니다: {profile.cohort.provider}"
        )
    path = resolve_cohort_patient_file(settings)
    seen: set[int | str] = set()
    yielded = 0
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name}:{line_number} JSON 파싱 실패: {exc}"
                ) from exc
            patient_key = _cohort_patient_key(
                resource,
                path,
                line_number,
                identifier_system=profile.cohort.identifier_system,
                key_type=profile.cohort.key_type,
            )
            if patient_key in seen:
                raise ValueError(
                    f"{path.name}:{line_number} patient_key가 중복됐습니다: {patient_key}"
                )
            seen.add(patient_key)
            yield patient_key
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def _paths(
    output_directory: Path, patient_key: int | str
) -> tuple[Path, Path]:
    safe_key = quote(str(patient_key), safe="")
    stem = output_directory / f"patient-{safe_key}"
    return stem.with_suffix(".bundle.json"), stem.with_suffix(".validation.json")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dump_json(value), encoding="utf-8")
    temporary.replace(path)


def _resource_counts(bundle: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in bundle.get("entry", []):
        resource_type = entry.get("resource", {}).get("resourceType")
        if resource_type:
            counts[resource_type] = counts.get(resource_type, 0) + 1
    return counts


def _aggregate(records: list[dict[str, Any]], output_directory: Path) -> dict[str, Any]:
    valid = sum(record.get("status") == "valid" for record in records)
    invalid = sum(record.get("status") == "invalid" for record in records)
    failed = sum(record.get("status") == "failed" for record in records)
    skipped = sum(bool(record.get("resumed")) for record in records)
    resources: dict[str, int] = {}
    issues = {"fatal": 0, "error": 0, "warning": 0, "information": 0}
    evaluation_values: dict[str, list[float]] = {
        "source_field_accuracy": [],
        "clinical_resource_macro_f1": [],
        "reference_accuracy": [],
        "coding_accuracy": [],
    }
    for record in records:
        for resource_type, count in record.get("resource_counts", {}).items():
            resources[resource_type] = resources.get(resource_type, 0) + int(count)
        for severity, count in record.get("validator_issue_counts", {}).items():
            issues[severity] = issues.get(severity, 0) + int(count)
        evaluation = record.get("task_evaluation") or {}
        candidates = {
            "source_field_accuracy": evaluation.get(
                "source_field_accuracy", {}
            ).get("accuracy"),
            "clinical_resource_macro_f1": evaluation.get(
                "clinical_resource_macro_f1"
            ),
            "reference_accuracy": evaluation.get(
                "reference_accuracy", {}
            ).get("all", {}).get("accuracy"),
            "coding_accuracy": evaluation.get(
                "terminology_accuracy", {}
            ).get("coding_system_and_code", {}).get("accuracy"),
        }
        for name, value in candidates.items():
            if isinstance(value, (int, float)):
                evaluation_values[name].append(float(value))
    return {
        "output_directory": str(output_directory),
        "attempted_patients": len(records),
        "generated_patients": valid + invalid,
        "valid_patients": valid,
        "invalid_patients": invalid,
        "failed_patients": failed,
        "resumed_patients": skipped,
        "resource_counts": resources,
        "validator_issue_counts": issues,
        "mean_task_evaluation": {
            name: (sum(values) / len(values) if values else None)
            for name, values in evaluation_values.items()
        },
        "performance_evaluation": aggregate_performance_metrics(records),
        "records": records,
    }


class FhirBatchExportService:
    """Profile cohort 환자를 DB→LLM→FHIR로 변환하고 저장 파일을 검증합니다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _patient_keys(self, request: ExportRequest) -> Iterable[int | str]:
        if request.patient_keys:
            return request.patient_keys
        return iter_cohort_patient_ids(
            self._settings,
            limit=None if request.all_cohort_patients else request.limit,
        )

    def run(self, request: ExportRequest) -> dict[str, Any]:
        request.validate()
        output_directory = request.output_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = output_directory / "manifest.json"
        records: list[dict[str, Any]] = []
        cohort = (
            {
                "source": "explicit-patient-ids",
                "requested_patient_keys": list(request.patient_keys),
            }
            if request.patient_keys
            else {
                "source": "source-profile-fhir-patient-ndjson",
                "patient_file": str(
                    resolve_cohort_patient_file(self._settings)
                ),
                "limit": None if request.all_cohort_patients else request.limit,
            }
        )

        def manifest() -> dict[str, Any]:
            return {
                **_aggregate(records, output_directory),
                "cohort": cohort,
            }

        for patient_key in self._patient_keys(request):
            bundle_path, validation_path = _paths(output_directory, patient_key)
            trace_path = bundle_path.with_name(
                bundle_path.name.replace(".bundle.json", ".trace.json")
            )
            failure_path = bundle_path.with_name(
                bundle_path.name.replace(".bundle.json", ".failure.json")
            )
            if request.resume and bundle_path.is_file() and validation_path.is_file():
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                report = json.loads(validation_path.read_text(encoding="utf-8"))
                prior_evaluation = report.get("metrics", {}).get(
                    "task_evaluation"
                )
                task_passed = (
                    not isinstance(prior_evaluation, dict)
                    or prior_evaluation.get("task_quality_pass") is True
                )
                validator_compatible = (
                    self._settings.fhir_validator_jar is None
                    or report.get("external_validator_used") is True
                )
                if report.get("valid") and task_passed and validator_compatible:
                    records.append(
                        {
                            "patient_key": patient_key,
                            "status": "valid",
                            "resumed": True,
                            "bundle": str(bundle_path),
                            "validation": str(validation_path),
                            "resource_counts": _resource_counts(bundle),
                            "metrics": report.get("metrics", {}),
                            "validator_issue_counts": report.get(
                                "validator_issue_counts", {}
                            ),
                            "task_evaluation": prior_evaluation,
                        }
                    )
                    _write_json_atomic(manifest_path, manifest())
                    continue
            try:
                pipeline_settings = replace(
                    self._settings,
                    fhir_validator_jar=None,
                )
                result = LlmSqlToFhirPipeline(pipeline_settings).run(patient_key)
                _write_json_atomic(bundle_path, result.fhir_payload)
                trace_payload = (
                    result.trace_dict()
                    if hasattr(result, "trace_dict")
                    else {"mode": "test-or-external-pipeline", "status": "success"}
                )
                _write_json_atomic(trace_path, trace_payload)
    
                # 디스크에 기록된 실제 산출물을 다시 읽어 독립 검증합니다.
                persisted_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                report = validate_bundle_detailed(
                    persisted_bundle, self._settings
                ).as_dict()
                task_evaluation = getattr(result, "task_evaluation", None)
                if task_evaluation is not None:
                    report["metrics"]["task_evaluation"] = task_evaluation
                    report["metrics"]["performance_evaluation"] = (
                        build_performance_metrics(report, task_evaluation)
                    )
                _write_json_atomic(validation_path, report)
                failure_path.unlink(missing_ok=True)
                records.append(
                    {
                        "patient_key": patient_key,
                        "status": "valid" if report["valid"] else "invalid",
                        "resumed": False,
                        "llm_calls_used": result.llm_calls_used,
                        "bundle": str(bundle_path),
                        "validation": str(validation_path),
                        "trace": str(trace_path),
                        "source_row_counts": getattr(
                            result, "source_row_counts", {}
                        ),
                        "rendering_metrics": getattr(
                            result, "rendering_metrics", {}
                        ),
                        "sql_generation_metrics": trace_payload.get(
                            "sql_generation_metrics", {}
                        ),
                        "resource_counts": _resource_counts(persisted_bundle),
                        "metrics": report["metrics"],
                        "task_evaluation": task_evaluation,
                        "validator_issue_counts": report[
                            "validator_issue_counts"
                        ],
                    }
                )
            except Exception as exc:
                failure_trace = (
                    exc.trace_dict()
                    if isinstance(exc, PipelineExecutionError)
                    else {
                        "status": "failed",
                        "failure": {
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
                _write_json_atomic(failure_path, failure_trace)
                records.append({
                    "patient_key": patient_key,
                    "status": "failed",
                    "resumed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "failure_trace": str(failure_path),
                    "failure": failure_trace.get("failure"),
                    "source_row_counts": failure_trace.get(
                        "source_row_counts", {}
                    ),
                    "rendering_metrics": failure_trace.get(
                        "rendering_metrics", {}
                    ),
                    "sql_generation_metrics": failure_trace.get(
                        "sql_generation_metrics", {}
                    ),
                })
            _write_json_atomic(manifest_path, manifest())
        summary = manifest()
        _write_json_atomic(manifest_path, summary)
        return {**summary, "manifest": str(manifest_path)}


def validate_export_directory(settings: Settings, directory: Path) -> dict[str, Any]:
    """이미 생성된 환자 Bundle들을 다시 읽고 현재 검증 정책으로 재검증합니다."""

    output_directory = directory.resolve()
    records: list[dict[str, Any]] = []
    for bundle_path in sorted(output_directory.glob("patient-*.bundle.json")):
        subject_text = unquote(
            bundle_path.name.removeprefix("patient-").removesuffix(
                ".bundle.json"
            )
        )
        validation_path = bundle_path.with_name(
            bundle_path.name.replace(".bundle.json", ".validation.json")
        )
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            prior_task_evaluation = None
            if validation_path.is_file():
                prior_report = json.loads(
                    validation_path.read_text(encoding="utf-8")
                )
                prior_task_evaluation = prior_report.get("metrics", {}).get(
                    "task_evaluation"
                )
            report = validate_bundle_detailed(bundle, settings).as_dict()
            if prior_task_evaluation is not None:
                report["metrics"]["task_evaluation"] = prior_task_evaluation
                report["metrics"]["performance_evaluation"] = (
                    build_performance_metrics(report, prior_task_evaluation)
                )
            _write_json_atomic(validation_path, report)
            records.append(
                {
                    "patient_key": subject_text,
                    "status": "valid" if report["valid"] else "invalid",
                    "resumed": False,
                    "bundle": str(bundle_path),
                    "validation": str(validation_path),
                    "resource_counts": _resource_counts(bundle),
                    "metrics": report["metrics"],
                    "task_evaluation": prior_task_evaluation,
                    "validator_issue_counts": report["validator_issue_counts"],
                }
            )
        except Exception as exc:
            records.append(
                {
                    "patient_key": subject_text,
                    "status": "failed",
                    "resumed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    summary = _aggregate(records, output_directory)
    report_path = output_directory / "revalidation.json"
    _write_json_atomic(report_path, summary)
    return {**summary, "report": str(report_path)}
