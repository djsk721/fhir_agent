from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from .config import Settings


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_engine(settings: Settings) -> Engine:
    """설정된 별도 원천 PostgreSQL 서버의 SQLAlchemy 엔진을 만듭니다."""

    connect_args = (
        {"connect_timeout": settings.db_connect_timeout_seconds}
        if settings.source_db_url.startswith("postgresql")
        else {}
    )
    return create_engine(
        settings.source_db_url,
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


@dataclass(frozen=True)
class SourceColumn:
    name: str
    data_type: str
    primary_key: bool


@dataclass(frozen=True)
class SourceForeignKey:
    columns: tuple[str, ...]
    referred_schema: str
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True)
class SourceTable:
    schema: str
    name: str
    columns: tuple[SourceColumn, ...]
    foreign_keys: tuple[SourceForeignKey, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def schema_text(self) -> str:
        columns = ", ".join(
            f"{column.name} {column.data_type}"
            f"{' PRIMARY KEY' if column.primary_key else ''}"
            for column in self.columns
        )
        foreign_keys = "; ".join(
            f"{','.join(key.columns)} -> "
            f"{key.referred_schema}.{key.referred_table}"
            f"({','.join(key.referred_columns)})"
            for key in self.foreign_keys
        )
        return (
            f"PostgreSQL table {self.qualified_name}\n"
            f"Columns: {columns}\n"
            f"Foreign keys: {foreign_keys or 'none'}"
        )


@dataclass(frozen=True)
class SourceSchemaCatalog:
    """SQL 허용, RAG 문서와 Self-Correction이 공유하는 DB metadata snapshot."""

    tables: tuple[SourceTable, ...]

    @classmethod
    def load(cls, settings: Settings, engine: Engine) -> "SourceSchemaCatalog":
        inspector = inspect(engine)
        tables: list[SourceTable] = []
        schemas = [
            schema
            for schema in inspector.get_schema_names()
            if schema.lower() not in {"public", "information_schema"}
            and not schema.lower().startswith("pg_")
        ]
        for schema in schemas:
            for table_name in inspector.get_table_names(schema=schema):
                raw_columns = inspector.get_columns(table_name, schema=schema)
                primary_key = set(
                    (
                        inspector.get_pk_constraint(table_name, schema=schema)
                        or {}
                    ).get("constrained_columns")
                    or []
                )
                raw_foreign_keys: list[dict[str, Any]] = (
                    inspector.get_foreign_keys(table_name, schema=schema)
                )
                tables.append(
                    SourceTable(
                        schema=schema,
                        name=table_name,
                        columns=tuple(
                            SourceColumn(
                                name=str(column["name"]),
                                data_type=str(column["type"]),
                                primary_key=column["name"] in primary_key,
                            )
                            for column in raw_columns
                        ),
                        foreign_keys=tuple(
                            SourceForeignKey(
                                columns=tuple(
                                    str(value)
                                    for value in key.get(
                                        "constrained_columns"
                                    )
                                    or []
                                ),
                                referred_schema=str(
                                    key.get("referred_schema") or schema
                                ),
                                referred_table=str(key.get("referred_table")),
                                referred_columns=tuple(
                                    str(value)
                                    for value in key.get(
                                        "referred_columns"
                                    )
                                    or []
                                ),
                            )
                            for key in raw_foreign_keys
                        ),
                    )
                )
        return cls(tuple(sorted(tables, key=lambda item: item.qualified_name)))

    @property
    def relations(self) -> tuple[tuple[str, str], ...]:
        return tuple((table.schema, table.name) for table in self.tables)

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(table.name.lower() for table in self.tables)

    @property
    def schemas(self) -> frozenset[str]:
        return frozenset(table.schema.lower() for table in self.tables)

    @property
    def relation_names(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (table.schema.lower(), table.name.lower()) for table in self.tables
        )

    def get(self, schema: str, table: str) -> SourceTable | None:
        key = (schema.lower(), table.lower())
        return next(
            (
                item
                for item in self.tables
                if (item.schema.lower(), item.name.lower()) == key
            ),
            None,
        )

    def schema_hint(
        self, relations: set[tuple[str, str]]
    ) -> str:
        selected = [
            table
            for schema, name in sorted(relations)
            if (table := self.get(schema, name)) is not None
        ]
        selected_names = {
            (table.schema.lower(), table.name.lower()) for table in selected
        }
        directly_related_names = {
            (
                foreign_key.referred_schema.lower(),
                foreign_key.referred_table.lower(),
            )
            for table in selected
            for foreign_key in table.foreign_keys
        }
        directly_related_names.update(
            (table.schema.lower(), table.name.lower())
            for table in self.tables
            for foreign_key in table.foreign_keys
            if (
                foreign_key.referred_schema.lower(),
                foreign_key.referred_table.lower(),
            )
            in selected_names
        )
        selected_columns = {
            column.lower()
            for table in selected
            for column in table.column_names
        }
        related = sorted(
            (
                table
                for table in self.tables
                if (table.schema.lower(), table.name.lower()) not in selected_names
                and (
                    (table.schema.lower(), table.name.lower())
                    in directly_related_names
                    or len(
                        selected_columns
                        & {column.lower() for column in table.column_names}
                    )
                    >= 2
                )
            ),
            key=lambda table: (
                -int(
                    (table.schema.lower(), table.name.lower())
                    in directly_related_names
                ),
                -max(
                    (
                        len(
                            set(re.split(r"[_\W]+", selected_table.name.lower()))
                            & set(re.split(r"[_\W]+", table.name.lower()))
                        )
                        for selected_table in selected
                    ),
                    default=0,
                ),
                -len(
                    selected_columns
                    & {column.lower() for column in table.column_names}
                ),
                table.qualified_name,
            ),
        )[:10]
        return "\n".join(
            table.schema_text()
            for table in [*selected, *related]
        )
