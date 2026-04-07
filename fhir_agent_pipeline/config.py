from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """`.env` 기반 애플리케이션 설정을 보관합니다."""

    mimic_db_url: str
    mimic_schema: str
    mimic_patients_table: str
    mimic_admissions_table: str
    mimic_labevents_table: str
    patient_subject_id: int | None
    ollama_base_url: str
    ollama_model: str
    fhir_output_resources: list[str]
    max_self_correction_attempts: int


def _parse_resource_list(raw_value: str) -> list[str]:
    """콤마 구분 문자열을 리소스 목록으로 정규화합니다."""

    values = [value.strip() for value in raw_value.split(",") if value.strip()]
    return values or ["Patient", "Encounter", "Observation"]


def load_settings(env_file: str | None = None) -> Settings:
    """환경 변수와 `.env` 파일을 읽어 설정 객체를 생성합니다."""

    load_dotenv(dotenv_path=env_file, override=False)

    mimic_db_url = os.getenv("MIMIC_DB_URL", "").strip()
    if not mimic_db_url:
        raise ValueError("MIMIC_DB_URL is required. See .env.example for the template.")

    patient_subject_id_raw = os.getenv("PATIENT_SUBJECT_ID", "").strip()
    patient_subject_id = int(patient_subject_id_raw) if patient_subject_id_raw else None
    resources_raw = os.getenv("FHIR_OUTPUT_RESOURCES", "").strip()
    legacy_resource = os.getenv("FHIR_OUTPUT_RESOURCE", "").strip()
    output_resources = _parse_resource_list(resources_raw or legacy_resource)

    return Settings(
        mimic_db_url=mimic_db_url,
        mimic_schema=os.getenv("MIMIC_SCHEMA", "mimiciv_hosp").strip() or "mimiciv_hosp",
        mimic_patients_table=os.getenv("MIMIC_PATIENTS_TABLE", "patients").strip() or "patients",
        mimic_admissions_table=os.getenv("MIMIC_ADMISSIONS_TABLE", "admissions").strip() or "admissions",
        mimic_labevents_table=os.getenv("MIMIC_LABEVENTS_TABLE", "labevents").strip() or "labevents",
        patient_subject_id=patient_subject_id,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip() or "http://localhost:11434",
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip() or "llama3.1:8b",
        fhir_output_resources=output_resources,
        max_self_correction_attempts=int(
            os.getenv("MAX_SELF_CORRECTION_ATTEMPTS", "2").strip() or "2"
        ),
    )

