from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import ast

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """`.env` 기반 애플리케이션 설정을 보관합니다."""

    mimic_db_url: str
    mimic_schema: str
    mimic_patients_table: str
    mimic_admissions_table: str
    mimic_labevents_table: str
    mimic_d_labitems_table: str
    patient_subject_id: int | None
    ollama_base_url: str
    ollama_model: str
    ollama_embed_model: str | None
    fhir_target_resources: list[str]
    fhir_allowed_tables: list[str]
    fhir_output_resources: list[str]
    max_self_correction_attempts: int
    rag_enabled: bool
    rag_top_k: int
    rag_fail_open: bool
    rag_embedding_provider: str
    rag_embedding_model: str
    fhir_schema_json_path: str
    fhir_schema_db_dir: str
    mimic_knowledge_db_dir: str
    sql_enabled: bool
    sql_fail_open: bool
    sql_row_limit: int
    sql_planner_retries: int


def _parse_bool(raw_value: str, default: bool) -> bool:
    value = raw_value.strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _parse_resource_list(raw_value: str) -> list[str]:
    """콤마 구분 문자열을 리소스 목록으로 정규화합니다."""

    values = _parse_string_list(raw_value)
    return values or ["Patient", "Encounter", "Observation"]


def _parse_string_list(raw_value: str) -> list[str]:
    value = raw_value.strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (SyntaxError, ValueError):
            pass
    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings(env_file: str | None = None) -> Settings:
    """환경 변수와 `.env` 파일을 읽어 설정 객체를 생성합니다."""

    load_dotenv(dotenv_path=env_file, override=False)

    mimic_db_url = os.getenv("MIMIC_DB_URL", "").strip()
    if not mimic_db_url:
        raise ValueError("MIMIC_DB_URL is required. See .env.example for the template.")

    patient_subject_id_raw = os.getenv("PATIENT_SUBJECT_ID", "").strip()
    patient_subject_id = int(patient_subject_id_raw) if patient_subject_id_raw else None
    resources_raw = os.getenv("FHIR_TARGET_RESOURCES", "").strip()
    if not resources_raw:
        resources_raw = os.getenv("FHIR_OUTPUT_RESOURCES", "").strip()
    legacy_resource = os.getenv("FHIR_OUTPUT_RESOURCE", "").strip()
    output_resources = _parse_resource_list(resources_raw or legacy_resource)
    allowed_tables = _parse_string_list(os.getenv("FHIR_ALLOWED_TABLES", ""))
    schema_json_default = str(Path("./db/fhir.schema.json"))

    return Settings(
        mimic_db_url=mimic_db_url,
        mimic_schema=os.getenv("MIMIC_SCHEMA", "mimiciv_hosp").strip()
        or "mimiciv_hosp",
        mimic_patients_table=os.getenv("MIMIC_PATIENTS_TABLE", "patients").strip()
        or "patients",
        mimic_admissions_table=os.getenv("MIMIC_ADMISSIONS_TABLE", "admissions").strip()
        or "admissions",
        mimic_labevents_table=os.getenv("MIMIC_LABEVENTS_TABLE", "labevents").strip()
        or "labevents",
        mimic_d_labitems_table=os.getenv("MIMIC_D_LABITEMS_TABLE", "d_labitems").strip()
        or "d_labitems",
        patient_subject_id=patient_subject_id,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
        or "http://localhost:11434",
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip() or "llama3.1:8b",
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "").strip() or None,
        fhir_target_resources=output_resources,
        fhir_allowed_tables=allowed_tables,
        fhir_output_resources=output_resources,
        max_self_correction_attempts=int(
            os.getenv("MAX_SELF_CORRECTION_ATTEMPTS", "2").strip() or "2"
        ),
        rag_enabled=_parse_bool(os.getenv("RAG_ENABLED", "true"), default=True),
        rag_top_k=int(os.getenv("RAG_TOP_K", "3").strip() or "3"),
        rag_fail_open=_parse_bool(os.getenv("RAG_FAIL_OPEN", "true"), default=True),
        rag_embedding_provider=os.getenv("RAG_EMBEDDING_PROVIDER", "huggingface")
        .strip()
        .lower()
        or "huggingface",
        rag_embedding_model=os.getenv(
            "RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        ).strip()
        or "sentence-transformers/all-MiniLM-L6-v2",
        fhir_schema_json_path=os.getenv(
            "FHIR_SCHEMA_JSON_PATH", schema_json_default
        ).strip()
        or schema_json_default,
        fhir_schema_db_dir=os.getenv(
            "FHIR_SCHEMA_DB_DIR", "./fhir_schema_vectordb"
        ).strip()
        or "./fhir_schema_vectordb",
        mimic_knowledge_db_dir=os.getenv(
            "MIMIC_KNOWLEDGE_DB_DIR", "./mimic_docs_vectordb"
        ).strip()
        or "./mimic_docs_vectordb",
        sql_enabled=_parse_bool(os.getenv("SQL_ENABLED", "true"), default=True),
        sql_fail_open=_parse_bool(os.getenv("SQL_FAIL_OPEN", "true"), default=True),
        sql_row_limit=int(os.getenv("SQL_ROW_LIMIT", "25").strip() or "25"),
        sql_planner_retries=int(os.getenv("SQL_PLANNER_RETRIES", "1").strip() or "1"),
    )
