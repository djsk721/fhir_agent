from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings


@dataclass(frozen=True)
class PatientDemoRow:
    """MIMIC-IV에서 추출한 환자 데모 데이터를 담습니다."""

    patient: dict[str, Any]
    admission: dict[str, Any] | None
    lab_event: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        """에이전트 입력용 직렬화 가능한 딕셔너리로 변환합니다."""

        return {
            "patient": self.patient,
            "admission": self.admission,
            "lab_event": self.lab_event,
        }


def build_engine(settings: Settings) -> Engine:
    """SQLAlchemy 엔진을 생성합니다."""

    return create_engine(settings.mimic_db_url, future=True, pool_pre_ping=True)


def _qualified_table(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _normalize_scalar(value: Any) -> Any:
    """DB 값을 JSON 직렬화 가능한 형태로 정규화합니다."""

    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _fetch_one_dict(engine: Engine, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    df = pd.read_sql_query(text(sql), con=engine, params=params)
    if df.empty:
        return None

    row = df.iloc[0].to_dict()
    return {str(key): _normalize_scalar(value) for key, value in row.items()}


def fetch_patient_demo_row(settings: Settings) -> PatientDemoRow:
    """`patients`, `admissions`, `labevents`에서 환자 1명의 데모 데이터를 읽습니다."""

    engine = build_engine(settings)
    patients_q = _qualified_table(settings.mimic_schema, settings.mimic_patients_table)
    admissions_q = _qualified_table(settings.mimic_schema, settings.mimic_admissions_table)
    labevents_q = _qualified_table(settings.mimic_schema, settings.mimic_labevents_table)

    if settings.patient_subject_id is not None:
        patient_sql = f"""
        SELECT *
        FROM {patients_q}
        WHERE subject_id = :subject_id
        LIMIT 1
        """
        patient = _fetch_one_dict(engine, patient_sql, {"subject_id": settings.patient_subject_id})
        if not patient:
            raise ValueError(f"No patient found for subject_id={settings.patient_subject_id}")
    else:
        patient_sql = f"""
        SELECT *
        FROM {patients_q}
        ORDER BY subject_id
        LIMIT 1
        """
        patient = _fetch_one_dict(engine, patient_sql, {})
        if not patient:
            raise ValueError("No patient rows were found in the patients table.")

    subject_id = patient["subject_id"]
    admission_sql = f"""
    SELECT *
    FROM {admissions_q}
    WHERE subject_id = :subject_id
    ORDER BY admittime DESC NULLS LAST
    LIMIT 1
    """
    admission = _fetch_one_dict(engine, admission_sql, {"subject_id": subject_id})

    lab_event: dict[str, Any] | None = None
    try:
        if admission and admission.get("hadm_id") is not None:
            labevent_sql = f"""
            SELECT *
            FROM {labevents_q}
            WHERE subject_id = :subject_id
              AND hadm_id = :hadm_id
            ORDER BY charttime DESC NULLS LAST
            LIMIT 1
            """
            lab_event = _fetch_one_dict(
                engine,
                labevent_sql,
                {"subject_id": subject_id, "hadm_id": admission["hadm_id"]},
            )
        else:
            labevent_sql = f"""
            SELECT *
            FROM {labevents_q}
            WHERE subject_id = :subject_id
            ORDER BY charttime DESC NULLS LAST
            LIMIT 1
            """
            lab_event = _fetch_one_dict(engine, labevent_sql, {"subject_id": subject_id})
    except SQLAlchemyError:
        # 관측 데이터 테이블이 없거나 접근 불가한 경우에도 파이프라인은 Patient/Encounter까지 진행합니다.
        lab_event = None

    return PatientDemoRow(patient=patient, admission=admission, lab_event=lab_event)

