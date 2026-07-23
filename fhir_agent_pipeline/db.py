from __future__ import annotations

import re

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .config import Settings


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_engine(settings: Settings) -> Engine:
    """설정된 별도 MIMIC PostgreSQL 서버의 SQLAlchemy 엔진을 만듭니다."""

    connect_args = (
        {"connect_timeout": settings.db_connect_timeout_seconds}
        if settings.mimic_db_url.startswith("postgresql")
        else {}
    )
    return create_engine(
        settings.mimic_db_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"안전하지 않은 PostgreSQL 식별자입니다: {value!r}")
    return f'"{value}"'


def qualified_table(schema: str, table: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"

