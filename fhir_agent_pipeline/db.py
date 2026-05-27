from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .config import Settings


@dataclass(frozen=True)
class PatientDemoRow:
    """MIMIC-IV에서 추출한 환자 데모 데이터를 담습니다."""

    patient: dict[str, Any]
    admission: dict[str, Any] | None
    lab_event: dict[str, Any] | None
    admissions: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        """에이전트 입력용 직렬화 가능한 딕셔너리로 변환합니다."""

        admissions = list(self.admissions or [])
        if self.admission is not None and not admissions:
            admissions = [self.admission]

        return {
            "patient": self.patient,
            "admission": self.admission if self.admission is not None else (admissions[0] if admissions else None),
            "admissions": admissions or None,
            "lab_event": self.lab_event,
        }


def build_engine(settings: Settings) -> Engine:
    """SQLAlchemy 엔진을 생성합니다."""

    return create_engine(settings.mimic_db_url, future=True, pool_pre_ping=True)


def _normalize_scalar(value: Any) -> Any:
    """DB 값을 JSON 직렬화 가능한 형태로 정규화합니다."""

    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
