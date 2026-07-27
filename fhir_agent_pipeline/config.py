from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re

from dotenv import load_dotenv

from .source_profile import SourceProfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """애플리케이션의 모든 설정. 값은 환경 변수 또는 `.env`에서 읽습니다."""

    source_db_url: str
    source_data_path: Path
    source_patient_id: int | str | None
    ollama_base_url: str
    ollama_model: str
    ollama_embedding_model: str
    ollama_timeout_seconds: int
    ollama_context_size: int
    ollama_max_output_tokens: int
    ollama_max_retries: int
    ollama_retry_delay_seconds: int
    llm_sql_max_attempts: int
    llm_fhir_max_attempts: int
    llm_fhir_batch_size: int
    llm_fhir_context_search_k: int
    embedding_dimension: int
    fhir_schema_path: Path
    vector_schema: str
    fhir_vector_table: str
    sql_vector_table: str
    vector_search_k: int
    vectors_enabled: bool
    sql_max_rows: int
    sql_statement_timeout_ms: int
    db_connect_timeout_seconds: int
    max_encounters: int
    max_observations_per_encounter: int
    max_conditions_per_encounter: int
    max_procedures_per_encounter: int
    fhir_output_resources: list[str]
    fhir_validator_jar: Path | None
    fhir_output_path: Path
    fhir_export_directory: Path
    fhir_default_timezone: str
    fhir_version: str
    fhir_validator_timeout_seconds: int
    fhir_validator_max_output_chars: int
    vector_batch_size: int
    source_profile_path: Path

def _csv(raw: str, default: list[str]) -> list[str]:
    values = [value.strip() for value in raw.split(",") if value.strip()]
    return values or default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}은 정수여야 합니다: {raw!r}") from exc


def _validate_settings(settings: Settings) -> None:
    bounded_values = {
        "EMBEDDING_DIMENSION": (settings.embedding_dimension, 1, None),
        "VECTOR_SEARCH_K": (settings.vector_search_k, 1, 100),
        "SQL_MAX_ROWS": (settings.sql_max_rows, 1, 10_000),
        "SQL_STATEMENT_TIMEOUT_MS": (settings.sql_statement_timeout_ms, 1, None),
        "DB_CONNECT_TIMEOUT_SECONDS": (settings.db_connect_timeout_seconds, 1, None),
        "SOURCE_MAX_ENCOUNTERS": (settings.max_encounters, 1, 100),
        "SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER": (
            settings.max_observations_per_encounter,
            1,
            10_000,
        ),
        "SOURCE_MAX_CONDITIONS_PER_ENCOUNTER": (
            settings.max_conditions_per_encounter,
            1,
            10_000,
        ),
        "SOURCE_MAX_PROCEDURES_PER_ENCOUNTER": (
            settings.max_procedures_per_encounter,
            1,
            10_000,
        ),
        "OLLAMA_TIMEOUT_SECONDS": (settings.ollama_timeout_seconds, 1, None),
        "OLLAMA_CONTEXT_SIZE": (settings.ollama_context_size, 1, None),
        "OLLAMA_MAX_OUTPUT_TOKENS": (settings.ollama_max_output_tokens, 1, None),
        "OLLAMA_MAX_RETRIES": (settings.ollama_max_retries, 0, 10),
        "OLLAMA_RETRY_DELAY_SECONDS": (
            settings.ollama_retry_delay_seconds,
            0,
            60,
        ),
        "LLM_SQL_MAX_ATTEMPTS": (settings.llm_sql_max_attempts, 1, 5),
        "LLM_FHIR_MAX_ATTEMPTS": (settings.llm_fhir_max_attempts, 1, 5),
        "LLM_FHIR_BATCH_SIZE": (settings.llm_fhir_batch_size, 1, 20),
        "LLM_FHIR_CONTEXT_SEARCH_K": (
            settings.llm_fhir_context_search_k,
            1,
            100,
        ),
        "FHIR_VALIDATOR_TIMEOUT_SECONDS": (
            settings.fhir_validator_timeout_seconds,
            1,
            None,
        ),
        "FHIR_VALIDATOR_MAX_OUTPUT_CHARS": (
            settings.fhir_validator_max_output_chars,
            1,
            None,
        ),
        "VECTOR_BATCH_SIZE": (settings.vector_batch_size, 1, 1_000),
    }
    for name, (value, minimum, maximum) in bounded_values.items():
        if value < minimum or (maximum is not None and value > maximum):
            upper = f"~{maximum}" if maximum is not None else " 이상"
            raise ValueError(f"{name}은 {minimum}{upper} 범위여야 합니다.")

    from .resource_registry import selected_adapters, supported_resource_types

    supported = supported_resource_types()
    unknown = set(settings.fhir_output_resources) - supported
    if unknown:
        raise ValueError(f"지원하지 않는 FHIR_OUTPUT_RESOURCES: {sorted(unknown)}")
    selected = set(settings.fhir_output_resources)
    missing_dependencies = {
        adapter.resource_type: sorted(set(adapter.dependencies) - selected)
        for adapter in selected_adapters(settings.fhir_output_resources)
        if set(adapter.dependencies) - selected
    }
    if missing_dependencies:
        raise ValueError(
            f"FHIR_OUTPUT_RESOURCES 의존 리소스 누락: {missing_dependencies}"
        )
    profile = SourceProfile.load(settings.source_profile_path)
    missing_queries = set(settings.fhir_output_resources) - set(profile.query_profiles)
    if missing_queries:
        raise ValueError(
            f"source profile query_profiles 리소스 누락: "
            f"{sorted(missing_queries)}"
        )
    if not re.fullmatch(r"Z|[+-](0[0-9]|1[0-4]):[0-5][0-9]", settings.fhir_default_timezone):
        raise ValueError("FHIR_DEFAULT_TIMEZONE은 Z 또는 ±HH:MM 형식이어야 합니다.")


def load_settings(env_file: str | None = None, require_database: bool = True) -> Settings:
    """설정을 로드하고 사람이 이해하기 쉬운 오류로 잘못된 값을 차단합니다."""

    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)
    source_profile_raw = os.getenv("SOURCE_PROFILE_PATH", "").strip()
    if not source_profile_raw:
        raise ValueError(
            "SOURCE_PROFILE_PATH가 필요합니다. "
            "사용할 데이터 소스의 profile.json 경로를 지정하세요."
        )
    source_profile_path = Path(source_profile_raw).expanduser().resolve()
    profile = SourceProfile.load(source_profile_path)

    def profile_value(generic_name: str, default: str) -> str:
        return os.getenv(generic_name, "").strip() or default

    source_db_url = profile_value("SOURCE_DB_URL", "")
    if require_database and not source_db_url:
        raise ValueError("SOURCE_DB_URL이 필요합니다.")

    subject_raw = os.getenv("SOURCE_PATIENT_ID", "").strip()
    validator_raw = os.getenv("FHIR_VALIDATOR_JAR", "").strip()
    resources_raw = os.getenv("FHIR_OUTPUT_RESOURCES", "").strip()

    settings = Settings(
        source_db_url=source_db_url,
        source_data_path=Path(
            profile_value("SOURCE_DATA_PATH", str(PROJECT_ROOT / "data" / profile.name))
        ).expanduser().resolve(),
        source_patient_id=(
            (
                int(subject_raw)
                if profile.cohort.key_type == "integer"
                else subject_raw
            )
            if subject_raw
            else None
        ),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:e4b").strip(),
        ollama_embedding_model=os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text").strip(),
        ollama_timeout_seconds=_integer("OLLAMA_TIMEOUT_SECONDS", 300),
        ollama_context_size=_integer("OLLAMA_CONTEXT_SIZE", 8192),
        ollama_max_output_tokens=_integer("OLLAMA_MAX_OUTPUT_TOKENS", 4096),
        ollama_max_retries=_integer("OLLAMA_MAX_RETRIES", 2),
        ollama_retry_delay_seconds=_integer("OLLAMA_RETRY_DELAY_SECONDS", 2),
        llm_sql_max_attempts=_integer("LLM_SQL_MAX_ATTEMPTS", 3),
        llm_fhir_max_attempts=_integer("LLM_FHIR_MAX_ATTEMPTS", 2),
        llm_fhir_batch_size=_integer("LLM_FHIR_BATCH_SIZE", 2),
        llm_fhir_context_search_k=_integer("LLM_FHIR_CONTEXT_SEARCH_K", 20),
        embedding_dimension=_integer("EMBEDDING_DIMENSION", 768),
        fhir_schema_path=_path("FHIR_SCHEMA_PATH", PROJECT_ROOT / "db" / "fhir.schema.json"),
        vector_schema=os.getenv("VECTOR_SCHEMA", "public").strip() or "public",
        fhir_vector_table=os.getenv("FHIR_VECTOR_TABLE", "fhir_schema_vectors").strip(),
        sql_vector_table=os.getenv("SQL_VECTOR_TABLE", "source_sql_schema_vectors").strip(),
        vector_search_k=_integer("VECTOR_SEARCH_K", 6),
        vectors_enabled=_bool("VECTORS_ENABLED", True),
        sql_max_rows=_integer("SQL_MAX_ROWS", 100),
        sql_statement_timeout_ms=_integer("SQL_STATEMENT_TIMEOUT_MS", 15_000),
        db_connect_timeout_seconds=_integer("DB_CONNECT_TIMEOUT_SECONDS", 5),
        max_encounters=_integer("SOURCE_MAX_ENCOUNTERS", 1),
        max_observations_per_encounter=_integer(
            "SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER", 10
        ),
        max_conditions_per_encounter=_integer(
            "SOURCE_MAX_CONDITIONS_PER_ENCOUNTER", 10
        ),
        max_procedures_per_encounter=_integer(
            "SOURCE_MAX_PROCEDURES_PER_ENCOUNTER", 10
        ),
        fhir_output_resources=_csv(
            resources_raw,
            ["Patient", "Encounter", "Condition", "Procedure", "Observation"],
        ),
        fhir_validator_jar=Path(validator_raw).expanduser().resolve() if validator_raw else None,
        fhir_output_path=_path("FHIR_OUTPUT_PATH", PROJECT_ROOT / "fhir_output" / "bundle.json"),
        fhir_export_directory=_path(
            "FHIR_EXPORT_DIR", PROJECT_ROOT / "fhir_export"
        ),
        fhir_default_timezone=os.getenv("FHIR_DEFAULT_TIMEZONE", "Z").strip() or "Z",
        fhir_version=os.getenv("FHIR_VERSION", "4.0.1").strip() or "4.0.1",
        fhir_validator_timeout_seconds=_integer("FHIR_VALIDATOR_TIMEOUT_SECONDS", 120),
        fhir_validator_max_output_chars=_integer(
            "FHIR_VALIDATOR_MAX_OUTPUT_CHARS", 10_000
        ),
        vector_batch_size=_integer("VECTOR_BATCH_SIZE", 64),
        source_profile_path=source_profile_path,
    )
    _validate_settings(settings)
    return settings
