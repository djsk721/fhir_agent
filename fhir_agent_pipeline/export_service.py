from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Settings
from .json_utils import dump_json
from .llm_pipeline import LlmSqlToFhirPipeline
from .performance import (
    aggregate_performance_metrics,
    build_performance_metrics,
)
from .validation import validate_bundle_detailed


@dataclass(frozen=True)
class ExportRequest:
    output_directory: Path
    subject_ids: tuple[int, ...] = ()
    limit: int | None = None
    all_demo_patients: bool = False
    resume: bool = False

    def validate(self) -> None:
        selectors = sum(
            (bool(self.subject_ids), self.limit is not None, self.all_demo_patients)
        )
        if selectors != 1:
            raise ValueError(
                "subject_ids, limit, all_demo_patients 중 하나만 지정해야 합니다."
            )
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")


def resolve_demo_patient_file(root: Path) -> Path:
    """demo 루트 또는 버전 상위 루트에서 Patient NDJSON gzip을 찾습니다."""

    candidates = [
        root / "MimicPatient.ndjson.gz",
        root / "fhir" / "MimicPatient.ndjson.gz",
        *root.glob("*/fhir/MimicPatient.ndjson.gz"),
    ]
    available = [path for path in candidates if path.is_file()]
    if not available:
        raise FileNotFoundError(
            f"demo Patient NDJSON을 찾을 수 없습니다: {root}"
        )

    def version_key(path: Path) -> tuple[int, ...]:
        version = path.parent.parent.name if path.parent.name == "fhir" else "0"
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError:
            return (0,)

    return max(available, key=version_key)


def _demo_subject_id(resource: dict[str, Any], path: Path, line_number: int) -> int:
    if resource.get("resourceType") != "Patient":
        raise ValueError(f"{path.name}:{line_number}은 Patient 리소스가 아닙니다.")
    identifiers = resource.get("identifier", [])
    numeric_values: set[int] = set()
    for identifier in identifiers if isinstance(identifiers, list) else []:
        if not isinstance(identifier, dict):
            continue
        raw_value = str(identifier.get("value", "")).strip()
        if raw_value.isdigit():
            numeric_values.add(int(raw_value))
    if len(numeric_values) != 1:
        raise ValueError(
            f"{path.name}:{line_number}에서 DB subject_id 숫자 identifier를 "
            f"하나로 결정할 수 없습니다: {sorted(numeric_values)}"
        )
    return numeric_values.pop()


def iter_demo_subject_ids(settings: Settings, limit: int | None) -> Iterator[int]:
    """demo Patient NDJSON의 identifier.value에서 DB subject_id를 읽습니다."""

    if limit is not None and limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다.")
    path = resolve_demo_patient_file(settings.mimic_fhir_demo_path)
    seen: set[int] = set()
    yielded = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name}:{line_number} JSON 파싱 실패: {exc}"
                ) from exc
            subject_id = _demo_subject_id(resource, path, line_number)
            if subject_id in seen:
                raise ValueError(
                    f"{path.name}:{line_number} subject_id가 중복됐습니다: {subject_id}"
                )
            seen.add(subject_id)
            yield subject_id
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def _paths(output_directory: Path, subject_id: int) -> tuple[Path, Path]:
    stem = output_directory / f"subject-{subject_id}"
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
    """demo cohort 환자를 DB→LLM→FHIR로 변환하고 저장 파일을 검증합니다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _subject_ids(self, request: ExportRequest) -> Iterable[int]:
        if request.subject_ids:
            return request.subject_ids
        return iter_demo_subject_ids(
            self._settings,
            limit=None if request.all_demo_patients else request.limit,
        )

    def run(self, request: ExportRequest) -> dict[str, Any]:
        request.validate()
        output_directory = request.output_directory.resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = output_directory / "manifest.json"
        records: list[dict[str, Any]] = []
        cohort = (
            {
                "source": "explicit-subject-ids",
                "requested_subject_ids": list(request.subject_ids),
            }
            if request.subject_ids
            else {
                "source": "mimic-fhir-demo-patient-ndjson",
                "patient_file": str(
                    resolve_demo_patient_file(self._settings.mimic_fhir_demo_path)
                ),
                "limit": None if request.all_demo_patients else request.limit,
            }
        )

        def manifest() -> dict[str, Any]:
            return {
                **_aggregate(records, output_directory),
                "cohort": cohort,
            }

        for subject_id in self._subject_ids(request):
            bundle_path, validation_path = _paths(output_directory, subject_id)
            if request.resume and bundle_path.is_file() and validation_path.is_file():
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                report = json.loads(validation_path.read_text(encoding="utf-8"))
                records.append(
                    {
                        "subject_id": subject_id,
                        "status": "valid" if report.get("valid") else "invalid",
                        "resumed": True,
                        "bundle": str(bundle_path),
                        "validation": str(validation_path),
                        "resource_counts": _resource_counts(bundle),
                        "metrics": report.get("metrics", {}),
                        "validator_issue_counts": report.get(
                            "validator_issue_counts", {}
                        ),
                        "task_evaluation": report.get("metrics", {}).get(
                            "task_evaluation"
                        ),
                    }
                )
                _write_json_atomic(manifest_path, manifest())
                continue
            try:
                result = LlmSqlToFhirPipeline(self._settings).run(subject_id)
                _write_json_atomic(bundle_path, result.fhir_payload)
    
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
                records.append(
                    {
                        "subject_id": subject_id,
                        "status": "valid" if report["valid"] else "invalid",
                        "resumed": False,
                        "llm_calls_used": result.llm_calls_used,
                        "bundle": str(bundle_path),
                        "validation": str(validation_path),
                        "resource_counts": _resource_counts(persisted_bundle),
                        "metrics": report["metrics"],
                        "task_evaluation": task_evaluation,
                        "validator_issue_counts": report[
                            "validator_issue_counts"
                        ],
                    }
                )
            except Exception as exc:
                records.append(
                    {
                        "subject_id": subject_id,
                        "status": "failed",
                        "resumed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            _write_json_atomic(manifest_path, manifest())
        summary = manifest()
        _write_json_atomic(manifest_path, summary)
        return {**summary, "manifest": str(manifest_path)}


def validate_export_directory(settings: Settings, directory: Path) -> dict[str, Any]:
    """이미 생성된 환자 Bundle들을 다시 읽고 현재 검증 정책으로 재검증합니다."""

    output_directory = directory.resolve()
    records: list[dict[str, Any]] = []
    for bundle_path in sorted(output_directory.glob("subject-*.bundle.json")):
        subject_text = bundle_path.name.removeprefix("subject-").removesuffix(
            ".bundle.json"
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
                    "subject_id": int(subject_text),
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
                    "subject_id": subject_text,
                    "status": "failed",
                    "resumed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    summary = _aggregate(records, output_directory)
    report_path = output_directory / "revalidation.json"
    _write_json_atomic(report_path, summary)
    return {**summary, "report": str(report_path)}
