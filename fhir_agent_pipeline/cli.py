from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable

from sqlalchemy import text

from .config import Settings, load_settings
from .db import build_engine, SourceSchemaCatalog
from .export_service import (
    ExportRequest,
    FhirBatchExportService,
    resolve_cohort_patient_file,
    validate_export_directory,
)
from .json_utils import dump_json
from .llm_pipeline import LlmSqlToFhirPipeline
from .performance import compare_rag_performance
from .source_profile import SourceProfile
from .sql_agent import NaturalLanguageSqlAgent
from .validation import validate_bundle_detailed
from .vector_store import PgVectorKnowledgeStore, build_knowledge_bases


def _print_json(value: Any) -> None:
    print(dump_json(value))


def _save_conversion(
    settings: Settings,
    fhir_payload: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, str]:
    bundle_path = settings.fhir_output_path
    report_path = bundle_path.with_suffix(".validation.json")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(dump_json(fhir_payload), encoding="utf-8")
    report_path.write_text(dump_json(validation), encoding="utf-8")
    return {"bundle": str(bundle_path), "validation": str(report_path)}


def convert(
    patient_key: int | str | None = None,
    encounter_key: int | str | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    profile = SourceProfile.load(settings.source_profile_path)

    def normalize(value: int | str | None) -> int | str | None:
        if value is None or not isinstance(value, str):
            return value
        return int(value) if profile.cohort.key_type == "integer" else value

    selected_patient_key = (
        normalize(patient_key)
        if patient_key is not None
        else settings.source_patient_id
    )
    if selected_patient_key is None:
        raise ValueError(
            "LLM 변환에는 --patient-key 또는 SOURCE_PATIENT_ID가 필요합니다."
        )
    result = LlmSqlToFhirPipeline(settings).run(
        selected_patient_key, normalize(encounter_key)
    )
    generation_mode = "llm-sql-and-llm-fhir"
    trace = result.trace_dict()
    validation = (
        result.validation_report.as_dict()
        if result.validation_report
        else {"valid": False, "errors": result.verification_errors}
    )
    output_files = _save_conversion(settings, result.fhir_payload, validation)
    trace_path = settings.fhir_output_path.with_suffix(".trace.json")
    trace_path.write_text(dump_json(trace), encoding="utf-8")
    output_files["trace"] = str(trace_path)
    output = {
        "bundle": result.fhir_payload,
        "validation": validation,
        "llm_calls_used": result.llm_calls_used,
        "generation_mode": generation_mode,
        "source_row_counts": getattr(result, "source_row_counts", None),
        "task_evaluation": getattr(result, "task_evaluation", None),
        "performance_evaluation": validation.get("metrics", {}).get(
            "performance_evaluation"
        ),
        "queries": [
            asdict(item) for item in getattr(result, "query_traces", [])
        ],
        "output_files": output_files,
    }
    _print_json(output)
    if result.verification_errors:
        raise RuntimeError("FHIR 검증 실패: " + "; ".join(result.verification_errors))
    return output


def ask(question: str) -> dict[str, Any]:
    output = asdict(NaturalLanguageSqlAgent(load_settings()).ask(question))
    _print_json(output)
    return output


def build_vectors() -> dict[str, Any]:
    output = build_knowledge_bases(load_settings())
    _print_json(output)
    return output


def inspect_vectors() -> list[dict[str, Any]]:
    settings = load_settings()
    output = PgVectorKnowledgeStore(settings).inspect_all()
    _print_json(output)
    return output


def validate_file(path: str) -> dict[str, Any]:
    settings = load_settings(require_database=False)
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    output = validate_bundle_detailed(payload, settings).as_dict()
    _print_json(output)
    return output


def _database_diagnostic(settings: Settings) -> str | dict[str, Any]:
    if not settings.source_db_url:
        return "not configured"
    try:
        engine = build_engine(settings)
        try:
            with engine.connect() as connection:
                database = str(
                    connection.execute(text("SELECT current_database()")).scalar_one()
                )
            catalog = SourceSchemaCatalog.load(settings, engine)
            return {
                "status": "connected",
                "database": database,
                "excluded_schemas": [
                    "public",
                    "information_schema",
                    "pg_*",
                ],
                "discovered_schemas": sorted(catalog.schemas),
                "table_selection": "all-non-public-user-schemas",
                "discovered_table_count": len(catalog.tables),
                "discovered_tables": [
                    table.qualified_name for table in catalog.tables
                ],
            }
        finally:
            engine.dispose()
    except ModuleNotFoundError as exc:
        return {
            "status": "connection failed",
            "error": "ModuleNotFoundError",
            "missing_module": exc.name,
            "hint": f"{sys.executable} -m pip install psycopg2-binary",
        }
    except Exception as exc:
        return {
            "status": "connection failed",
            "error": type(exc).__name__,
            "message": str(exc).splitlines()[0],
        }


def doctor() -> dict[str, Any]:
    settings = load_settings(require_database=False)
    try:
        demo_patient_file = str(resolve_cohort_patient_file(settings))
    except (FileNotFoundError, ValueError) as exc:
        demo_patient_file = str(exc)
    result: dict[str, Any] = {
        "source_profile": str(settings.source_profile_path),
        "source_profile_exists": settings.source_profile_path.is_file(),
        "fhir_schema_path": str(settings.fhir_schema_path),
        "fhir_schema_exists": settings.fhir_schema_path.is_file(),
        "fhir_default_timezone": settings.fhir_default_timezone,
        "source_data_path": str(settings.source_data_path),
        "cohort_patient_file": demo_patient_file,
        "fhir_export_directory": str(settings.fhir_export_directory),
        "database": _database_diagnostic(settings),
        "ollama": settings.ollama_base_url,
        "postgres_driver": {
            "module": "psycopg2",
            "installed": importlib.util.find_spec("psycopg2") is not None,
            "install": f"{sys.executable} -m pip install psycopg2-binary",
        },
    }
    _print_json(result)
    return result


def export_validate(
    *,
    patient_keys: list[int | str] | None,
    limit: int | None,
    all_cohort_patients: bool,
    output_directory: str | None,
    resume: bool,
) -> dict[str, Any]:
    settings = load_settings()
    profile = SourceProfile.load(settings.source_profile_path)
    normalized_patient_keys = tuple(
        int(value) if profile.cohort.key_type == "integer" else str(value)
        for value in (patient_keys or ())
    )
    request = ExportRequest(
        output_directory=(
            Path(output_directory) if output_directory else settings.fhir_export_directory
        ),
        patient_keys=normalized_patient_keys,
        limit=limit,
        all_cohort_patients=all_cohort_patients,
        resume=resume,
    )
    output = FhirBatchExportService(settings).run(request)
    _print_json(output)
    if output["invalid_patients"] or output["failed_patients"]:
        raise RuntimeError(
            "배치 생성/검증 실패: "
            f"invalid={output['invalid_patients']}, failed={output['failed_patients']}"
        )
    return output


def validate_export(path: str | None) -> dict[str, Any]:
    settings = load_settings(require_database=False)
    output = validate_export_directory(
        settings, Path(path) if path else settings.fhir_export_directory
    )
    _print_json(output)
    if not output["attempted_patients"]:
        raise RuntimeError("검증할 patient-*.bundle.json 파일이 없습니다.")
    if output["invalid_patients"] or output["failed_patients"]:
        raise RuntimeError(
            "재검증 실패: "
            f"invalid={output['invalid_patients']}, failed={output['failed_patients']}"
        )
    return output


def _performance_from_file(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = [
        payload.get("performance_evaluation"),
        payload.get("metrics", {}).get("performance_evaluation"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    raise ValueError(
        f"{path}에서 performance_evaluation을 찾을 수 없습니다."
    )


def compare_rag(baseline_path: str, rag_path: str) -> dict[str, Any]:
    baseline = _performance_from_file(baseline_path)
    rag = _performance_from_file(rag_path)
    output = compare_rag_performance(baseline, rag)
    output["baseline_file"] = str(Path(baseline_path).resolve())
    output["rag_file"] = str(Path(rag_path).resolve())
    _print_json(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="임상 DB → FHIR 변환, 자연어 SQL, pgvector 관리 CLI"
    )
    commands = parser.add_subparsers(dest="command")
    convert_parser = commands.add_parser("convert", help="환자 1명의 FHIR Bundle 생성")
    convert_parser.add_argument("--patient-key")
    convert_parser.add_argument("--encounter-key")
    ask_parser = commands.add_parser("ask", help="자연어 질문을 안전한 SELECT로 조회")
    ask_parser.add_argument("question")
    commands.add_parser(
        "build-vectors", help="FHIR JSON Schema와 DB 스키마를 pgvector에 적재"
    )
    commands.add_parser("inspect-vectors", help="두 벡터 테이블 구조와 행 수 확인")
    validate_parser = commands.add_parser(
        "validate", help="FHIR Bundle JSON 검증과 품질 지표 출력"
    )
    validate_parser.add_argument("path")
    commands.add_parser("doctor", help="경로와 연결 설정 빠른 점검")
    export_parser = commands.add_parser(
        "export-validate", help="DB 추출 → 환자별 FHIR 생성 → 저장 파일 독립 검증"
    )
    selectors = export_parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument(
        "--patient-key", action="append", dest="patient_keys",
        help="특정 환자 ID. 여러 번 지정 가능",
    )
    selectors.add_argument(
        "--limit", type=int,
        help="source profile cohort 순서에서 처리할 환자 수",
    )
    selectors.add_argument(
        "--all", action="store_true", dest="all_cohort_patients",
        help="source profile cohort에 포함된 모든 환자를 처리",
    )
    export_parser.add_argument("--output-dir")
    export_parser.add_argument("--resume", action="store_true")
    revalidate_parser = commands.add_parser(
        "validate-export", help="생성된 환자별 Bundle 디렉터리 재검증"
    )
    revalidate_parser.add_argument("path", nargs="?")
    compare_parser = commands.add_parser(
        "compare-rag",
        help="동일 cohort의 비-RAG/RAG 성능 결과 비교",
    )
    compare_parser.add_argument("baseline")
    compare_parser.add_argument("rag")
    return parser


def main() -> Any:
    args = build_parser().parse_args()
    handlers: dict[str, Callable[[], Any]] = {
        "convert": lambda: convert(
            getattr(args, "patient_key", None),
            getattr(args, "encounter_key", None),
        ),
        "ask": lambda: ask(args.question),
        "build-vectors": build_vectors,
        "inspect-vectors": inspect_vectors,
        "validate": lambda: validate_file(args.path),
        "doctor": doctor,
        "export-validate": lambda: export_validate(
            patient_keys=args.patient_keys,
            limit=args.limit,
            all_cohort_patients=args.all_cohort_patients,
            output_directory=args.output_dir,
            resume=args.resume,
        ),
        "validate-export": lambda: validate_export(args.path),
        "compare-rag": lambda: compare_rag(args.baseline, args.rag),
    }
    return handlers[args.command or "convert"]()


def entrypoint() -> None:
    try:
        main()
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
