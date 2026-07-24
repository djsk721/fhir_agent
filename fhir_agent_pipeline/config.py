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

    mimic_db_url: str
    mimic_fhir_demo_path: Path
    mimic_schema: str
    mimic_patients_table: str
    mimic_admissions_table: str
    mimic_labevents_table: str
    mimic_diagnoses_table: str
    mimic_diagnosis_dictionary_table: str
    mimic_procedures_table: str
    mimic_procedure_dictionary_table: str
    allowed_schemas: tuple[str, ...]
    allowed_tables: tuple[str, ...]
    patient_subject_id: int | str | None
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
    mimic_mapping_config_path: Path
    fhir_default_timezone: str
    fhir_version: str
    fhir_validator_timeout_seconds: int
    fhir_validator_max_output_chars: int
    vector_batch_size: int
    source_profile_path: Path

    @property
    def source_db_url(self) -> str:
        return self.mimic_db_url

    @property
    def source_data_path(self) -> Path:
        return self.mimic_fhir_demo_path

    @property
    def source_schema(self) -> str:
        return self.mimic_schema

    @property
    def source_mapping_config_path(self) -> Path:
        return self.mimic_mapping_config_path

    def source_table(self, role: str) -> str:
        tables = {
            "patient": self.mimic_patients_table,
            "encounter": self.mimic_admissions_table,
            "observation": self.mimic_labevents_table,
            "condition": self.mimic_diagnoses_table,
            "condition_dictionary": self.mimic_diagnosis_dictionary_table,
            "procedure": self.mimic_procedures_table,
            "procedure_dictionary": self.mimic_procedure_dictionary_table,
        }
        try:
            return tables[role]
        except KeyError as exc:
            raise ValueError(f"알 수 없는 원천 테이블 역할: {role}") from exc


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


def _integer_alias(generic_name: str, legacy_name: str, default: int) -> int:
    raw = os.getenv(generic_name)
    return _integer(
        generic_name if raw is not None else legacy_name,
        default,
    )


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
    if not re.fullmatch(r"Z|[+-](0[0-9]|1[0-4]):[0-5][0-9]", settings.fhir_default_timezone):
        raise ValueError("FHIR_DEFAULT_TIMEZONE은 Z 또는 ±HH:MM 형식이어야 합니다.")


def load_settings(env_file: str | None = None, require_database: bool = True) -> Settings:
    """설정을 로드하고 사람이 이해하기 쉬운 오류로 잘못된 값을 차단합니다."""

    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)
    source_profile_path = _path(
        "SOURCE_PROFILE_PATH",
        PROJECT_ROOT / "profiles" / "mimic_iv" / "profile.json",
    )
    profile = SourceProfile.load(source_profile_path)
    is_mimic_profile = profile.name.lower().startswith("mimic")

    def profile_value(
        generic_name: str, legacy_name: str, default: str
    ) -> str:
        value = os.getenv(generic_name, "").strip()
        if not value and is_mimic_profile:
            value = os.getenv(legacy_name, "").strip()
        return value or default

    mimic_db_url = profile_value("SOURCE_DB_URL", "MIMIC_DB_URL", "")
    if require_database and not mimic_db_url:
        raise ValueError(
            "SOURCE_DB_URL이 필요합니다. 기존 MIMIC profile은 MIMIC_DB_URL도 지원합니다."
        )

    schema = profile_value("SOURCE_SCHEMA", "MIMIC_SCHEMA", profile.database_schema)
    patients = profile_value(
        "SOURCE_PATIENT_TABLE", "MIMIC_PATIENTS_TABLE", profile.table("patient")
    )
    admissions = profile_value(
        "SOURCE_ENCOUNTER_TABLE",
        "MIMIC_ADMISSIONS_TABLE",
        profile.table("encounter"),
    )
    labevents = profile_value(
        "SOURCE_OBSERVATION_TABLE",
        "MIMIC_LABEVENTS_TABLE",
        profile.table("observation"),
    )
    diagnoses = profile_value(
        "SOURCE_CONDITION_TABLE",
        "MIMIC_DIAGNOSES_TABLE",
        profile.table("condition"),
    )
    diagnosis_dictionary = profile_value(
        "SOURCE_CONDITION_DICTIONARY_TABLE",
        "MIMIC_DIAGNOSIS_DICTIONARY_TABLE",
        profile.table("condition_dictionary"),
    )
    procedures = profile_value(
        "SOURCE_PROCEDURE_TABLE",
        "MIMIC_PROCEDURES_TABLE",
        profile.table("procedure"),
    )
    procedure_dictionary = profile_value(
        "SOURCE_PROCEDURE_DICTIONARY_TABLE",
        "MIMIC_PROCEDURE_DICTIONARY_TABLE",
        profile.table("procedure_dictionary"),
    )
    subject_raw = os.getenv(
        "SOURCE_PATIENT_ID", os.getenv("PATIENT_SUBJECT_ID", "")
    ).strip()
    validator_raw = os.getenv("FHIR_VALIDATOR_JAR", "").strip()
    resources_raw = os.getenv("FHIR_OUTPUT_RESOURCES", "").strip()

    settings = Settings(
        mimic_db_url=mimic_db_url,
        mimic_fhir_demo_path=Path(
            profile_value(
                "SOURCE_DATA_PATH",
                "MIMIC_FHIR_DATA_PATH",
                str(PROJECT_ROOT / "data" / profile.name),
            )
        ).expanduser().resolve(),
        mimic_schema=schema,
        mimic_patients_table=patients,
        mimic_admissions_table=admissions,
        mimic_labevents_table=labevents,
        mimic_diagnoses_table=diagnoses,
        mimic_diagnosis_dictionary_table=diagnosis_dictionary,
        mimic_procedures_table=procedures,
        mimic_procedure_dictionary_table=procedure_dictionary,
        allowed_schemas=tuple(_csv(os.getenv("SQL_ALLOWED_SCHEMAS", schema), [schema])),
        allowed_tables=tuple(
            _csv(
                os.getenv(
                    "SQL_ALLOWED_TABLES",
                    (
                        f"{patients},{admissions},{labevents},{diagnoses},"
                        f"{diagnosis_dictionary},{procedures},{procedure_dictionary}"
                    ),
                ),
                [
                    patients,
                    admissions,
                    labevents,
                    diagnoses,
                    diagnosis_dictionary,
                    procedures,
                    procedure_dictionary,
                ],
            )
        ),
        patient_subject_id=(
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
        llm_sql_max_attempts=_integer("LLM_SQL_MAX_ATTEMPTS", 2),
        llm_fhir_max_attempts=_integer("LLM_FHIR_MAX_ATTEMPTS", 2),
        llm_fhir_batch_size=_integer("LLM_FHIR_BATCH_SIZE", 2),
        llm_fhir_context_search_k=_integer("LLM_FHIR_CONTEXT_SEARCH_K", 20),
        embedding_dimension=_integer("EMBEDDING_DIMENSION", 768),
        fhir_schema_path=_path("FHIR_SCHEMA_PATH", PROJECT_ROOT / "db" / "fhir.schema.json"),
        vector_schema=os.getenv("VECTOR_SCHEMA", "public").strip() or "public",
        fhir_vector_table=os.getenv("FHIR_VECTOR_TABLE", "fhir_mapping_vectors").strip(),
        sql_vector_table=os.getenv("SQL_VECTOR_TABLE", "mimic_sql_schema_vectors").strip(),
        vector_search_k=_integer("VECTOR_SEARCH_K", 6),
        vectors_enabled=_bool("VECTORS_ENABLED", True),
        sql_max_rows=_integer("SQL_MAX_ROWS", 100),
        sql_statement_timeout_ms=_integer("SQL_STATEMENT_TIMEOUT_MS", 15_000),
        db_connect_timeout_seconds=_integer("DB_CONNECT_TIMEOUT_SECONDS", 5),
        max_encounters=_integer_alias(
            "SOURCE_MAX_ENCOUNTERS", "MIMIC_MAX_ENCOUNTERS", 1
        ),
        max_observations_per_encounter=_integer_alias(
            "SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER",
            "MIMIC_MAX_OBSERVATIONS_PER_ENCOUNTER",
            10,
        ),
        max_conditions_per_encounter=_integer_alias(
            "SOURCE_MAX_CONDITIONS_PER_ENCOUNTER",
            "MIMIC_MAX_CONDITIONS_PER_ENCOUNTER",
            10,
        ),
        max_procedures_per_encounter=_integer_alias(
            "SOURCE_MAX_PROCEDURES_PER_ENCOUNTER",
            "MIMIC_MAX_PROCEDURES_PER_ENCOUNTER",
            10,
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
        mimic_mapping_config_path=Path(
            profile_value(
                "SOURCE_MAPPING_CONFIG",
                "MIMIC_MAPPING_CONFIG",
                str(profile.mapping_path),
            )
        ).expanduser().resolve(),
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
