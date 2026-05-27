from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .config import Settings
from .db import _normalize_scalar, build_engine
from .payload_utils import extract_admissions


class SubjectConsistencyError(ValueError):
    pass


@dataclass(frozen=True)
class SqlQueryPlan:
    resource_type: str
    sql: str
    params: dict[str, Any]
    tables: list[str]
    rationale: str
    expected_subject_id: int | str | None = None


@dataclass(frozen=True)
class SqlExecutionResult:
    resource_type: str
    sql: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    trace: list[dict[str, Any]]


class SafeSqlRuntime:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: Engine | None = None
        self._schema_cache: dict[str, list[dict[str, Any]]] = {}

    def allowed_tables_for_resource(self, resource_type: str) -> list[str]:
        resource_defaults = {
            "Patient": [self._settings.mimic_patients_table],
            "Encounter": [
                self._settings.mimic_admissions_table,
                self._settings.mimic_patients_table,
            ],
            "Observation": [
                self._settings.mimic_labevents_table,
                self._settings.mimic_admissions_table,
                self._settings.mimic_patients_table,
                self._settings.mimic_d_labitems_table,
            ],
        }
        configured_tables = self._settings.fhir_allowed_tables
        if configured_tables:
            return configured_tables
        return resource_defaults.get(
            resource_type, [self._settings.mimic_patients_table]
        )

    def build_sql_seed_context(
        self, raw_payload: dict[str, Any], resource_type: str
    ) -> dict[str, Any]:
        patient = raw_payload.get("patient") or {}
        admission = raw_payload.get("admission") or {}
        admissions = extract_admissions(raw_payload)
        lab_event = raw_payload.get("lab_event") or {}
        hadm_ids = [
            admission_row["hadm_id"]
            for admission_row in admissions
            if admission_row.get("hadm_id") is not None
        ]
        hadm_id = admission.get("hadm_id") or lab_event.get("hadm_id")
        if hadm_id is None and len(hadm_ids) == 1:
            hadm_id = hadm_ids[0]

        return {
            "subject_id": patient.get("subject_id"),
            "hadm_id": hadm_id,
            "hadm_ids": hadm_ids,
            "admission_count": len(admissions),
            "itemid": lab_event.get("itemid"),
            "resource_type": resource_type,
        }

    def build_schema_context(self, resource_type: str) -> str:
        summaries = self._introspect_allowed_tables(resource_type)
        if not summaries:
            return "No live DB schema metadata available. Use only clearly safe read-only SELECT queries."

        blocks = []
        for summary in summaries:
            columns = ", ".join(summary["columns"])
            blocks.append(
                f"Table {summary['table']} (schema={summary['schema']}): columns={columns}"
            )
        return "\n".join(blocks)

    def fetch_admissions_for_patient(self, subject_id: int | str | None) -> list[dict[str, Any]]:
        if subject_id is None:
            return []

        sql = (
            f"SELECT * FROM {self._settings.mimic_schema}.{self._settings.mimic_admissions_table} "
            "WHERE subject_id = :subject_id "
            "ORDER BY admittime NULLS LAST, hadm_id"
        )
        engine = self._get_engine()
        df = pd.read_sql_query(text(sql), con=engine, params={"subject_id": subject_id})
        return [
            {str(key): _normalize_scalar(value) for key, value in record.items()}
            for record in df.to_dict(orient="records")
        ]

    def execute_plan(self, plan: SqlQueryPlan) -> SqlExecutionResult:
        validated_sql = self._validate_and_normalize_sql(plan)
        engine = self._get_engine()
        df = pd.read_sql_query(text(validated_sql), con=engine, params=plan.params)
        rows = [
            {str(key): _normalize_scalar(value) for key, value in record.items()}
            for record in df.to_dict(orient="records")
        ]
        rows, discarded_count = _filter_rows_by_subject_id(rows, plan.expected_subject_id)
        trace = [
            {
                "type": "sql_plan",
                "resource_type": plan.resource_type,
                "tables": plan.tables,
                "sql": validated_sql,
                "params": plan.params,
                "status": "executed",
                "row_count": len(rows),
                "detail": plan.rationale,
            }
        ]
        if discarded_count > 0:
            trace.append(
                {
                    "type": "sql_plan",
                    "resource_type": plan.resource_type,
                    "status": "filtered",
                    "discarded_row_count": discarded_count,
                    "detail": "Discarded SQL evidence rows with subject_id inconsistent with the request subject_id.",
                }
            )
        return SqlExecutionResult(
            resource_type=plan.resource_type,
            sql=validated_sql,
            params=plan.params,
            rows=rows,
            trace=trace,
        )

    def _get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = build_engine(self._settings)
        return self._engine

    def _introspect_allowed_tables(self, resource_type: str) -> list[dict[str, Any]]:
        cache_key = resource_type
        if cache_key in self._schema_cache:
            return self._schema_cache[cache_key]

        try:
            inspector = inspect(self._get_engine())
            tables = []
            for table_name in self.allowed_tables_for_resource(resource_type):
                if not inspector.has_table(
                    table_name, schema=self._settings.mimic_schema
                ):
                    continue
                columns = [
                    str(column["name"])
                    for column in inspector.get_columns(
                        table_name,
                        schema=self._settings.mimic_schema,
                    )
                ]
                tables.append(
                    {
                        "schema": self._settings.mimic_schema,
                        "table": table_name,
                        "columns": columns,
                    }
                )
            self._schema_cache[cache_key] = tables
            return tables
        except Exception:
            self._schema_cache[cache_key] = []
            return []

    def _validate_and_normalize_sql(self, plan: SqlQueryPlan) -> str:
        sql = plan.sql.strip().rstrip(";")
        upper_sql = sql.upper()

        if not upper_sql.startswith("SELECT"):
            raise ValueError("Only SELECT statements are allowed.")
        if "--" in sql or "/*" in sql or ";" in sql:
            raise ValueError("SQL comments and multiple statements are not allowed.")
        forbidden = [
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "DROP ",
            "ALTER ",
            "CREATE ",
            "TRUNCATE ",
            "GRANT ",
            "REVOKE ",
        ]
        for keyword in forbidden:
            if keyword in upper_sql:
                raise ValueError(f"Forbidden SQL keyword detected: {keyword.strip()}")

        referenced_tables = self._extract_tables(sql)
        allowed_tables = set(self.allowed_tables_for_resource(plan.resource_type))
        allowed_qualified = {
            f"{self._settings.mimic_schema}.{table}" for table in allowed_tables
        }
        for table in referenced_tables:
            if table not in allowed_tables and table not in allowed_qualified:
                raise ValueError(
                    f"Table '{table}' is not allowed for {plan.resource_type}."
                )

        if plan.expected_subject_id is not None:
            subject_predicate = re.search(
                r"\bWHERE\b[\s\S]*?\bsubject_id\b\s*=\s*:subject_id\b",
                sql,
                re.IGNORECASE,
            )
            if subject_predicate is None:
                raise SubjectConsistencyError(
                    "SQL query must include WHERE subject_id = :subject_id for subject consistency."
                )
            if plan.params.get("subject_id") != plan.expected_subject_id:
                raise SubjectConsistencyError(
                    "SQL query subject_id parameter does not match the request subject_id."
                )

        limit_match = re.search(r"\bLIMIT\s+(\d+)\b", upper_sql)
        if limit_match:
            current_limit = int(limit_match.group(1))
            if current_limit > self._settings.sql_row_limit:
                raise ValueError("SQL LIMIT exceeds configured safety threshold.")
            return sql

        return f"{sql} LIMIT {self._settings.sql_row_limit}"

    def _extract_tables(self, sql: str) -> list[str]:
        pattern = re.compile(r"(?:FROM|JOIN)\s+([a-zA-Z0-9_\.]+)", re.IGNORECASE)
        return pattern.findall(sql)


def merge_sql_evidence(
    raw_payload: dict[str, Any], sql_evidence: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    return {
        **raw_payload,
        "sql_evidence": sql_evidence,
    }


def _filter_rows_by_subject_id(
    rows: list[dict[str, Any]], expected_subject_id: int | str | None
) -> tuple[list[dict[str, Any]], int]:
    if expected_subject_id is None:
        return rows, 0

    filtered_rows: list[dict[str, Any]] = []
    discarded_count = 0
    for row in rows:
        row_subject_id = row.get("subject_id")
        if row_subject_id is not None and str(row_subject_id) == str(expected_subject_id):
            filtered_rows.append(row)
        else:
            discarded_count += 1

    return filtered_rows, discarded_count
