from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from fhir_agent_pipeline.db import quote_identifier

from ..contracts import SourceBatch
from ..splits import _checked_keys


def load_extract_spec(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
        raise ValueError("추출 명세에는 resources 객체가 필요합니다.")
    return payload


def _source_structure(resource_spec: dict[str, Any]) -> dict[str, Any]:
    """Expose reviewed source keys and sample strata without inventing mappings."""
    metadata = {
        "primary_keys": list(resource_spec.get("primary_keys") or []),
        "foreign_keys": list(resource_spec.get("foreign_keys") or []),
        "column_descriptions": dict(resource_spec.get("column_descriptions") or {}),
    }
    if "table_description" in resource_spec:
        metadata["table_description"] = str(resource_spec["table_description"])
    for key in ("identity_fields", "sample_group_fields"):
        if key in resource_spec:
            fields = resource_spec[key]
            if (not isinstance(fields, list) or not fields
                    or any(not isinstance(field, str) or field not in resource_spec["columns"] for field in fields)
                    or len(set(fields)) != len(fields)):
                raise ValueError(f"{key}에는 중복 없는 명세 컬럼 목록이 필요합니다.")
            metadata[key] = list(fields)
    return metadata


def build_select(
    resource_spec: dict[str, Any], *, with_encounters: bool,
    patient_filter: str = "single", include_limit: bool = True, count: bool = False,
) -> str:
    """허용된 식별자만으로 매개변수화 SELECT를 구성합니다."""

    columns = resource_spec["columns"]
    if not isinstance(columns, list) or not columns:
        raise ValueError("추출 컬럼 목록이 비어 있습니다.")
    selected = "COUNT(*)" if count else ", ".join(quote_identifier(str(column)) for column in columns)
    table = quote_identifier(str(resource_spec["table"]))
    schema = resource_spec.get("schema")
    relation = (
        f"{quote_identifier(str(schema))}.{table}" if schema else table
    )
    patient_column = quote_identifier(str(resource_spec["patient_column"]))
    if patient_filter not in {"single", "multiple", "all"}:
        raise ValueError("patient_filter는 single/multiple/all이어야 합니다.")
    sql = f"SELECT {selected} FROM {relation}"
    conditions = []
    if patient_filter == "single":
        conditions.append(f"{patient_column} = :patient_key")
    elif patient_filter == "multiple":
        conditions.append(f"{patient_column} IN :patient_keys")
    if with_encounters:
        encounter_column = resource_spec.get("encounter_column")
        if not encounter_column:
            raise ValueError("Encounter 필터에는 encounter_column이 필요합니다.")
        conditions.append(f"{quote_identifier(str(encounter_column))} IN :encounter_keys")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    order_columns = resource_spec.get("primary_keys") or []
    if order_columns and not count:
        ordering = ", ".join(
            quote_identifier(str(column)) for column in order_columns
        )
        sql += f" ORDER BY {ordering}"
    limit = resource_spec.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("추출 limit은 1 이상의 정수여야 합니다.")
        if include_limit and not count:
            sql += f" LIMIT {limit}"
    return sql


class StaticRecordExtractor:
    """이미 고정된 Source Records를 연구 배치 계약으로 감쌉니다."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec

    def extract(
        self,
        resource_type: str,
        records: list[dict[str, Any]],
    ) -> SourceBatch:
        resource_spec = self.spec["resources"][resource_type]
        return SourceBatch(
            dataset=str(self.spec["dataset"]),
            dataset_version=str(self.spec["dataset_version"]),
            source_resource=str(resource_spec["source_resource"]),
            schema_version=str(self.spec["schema_version"]),
            records=tuple(dict(row) for row in records),
            metadata={
                "columns": list(resource_spec["columns"]),
                **_source_structure(resource_spec),
            },
            extraction={
                "extractor_version": str(self.spec.get("extractor_version", "v1")),
                "mode": "static-records",
            },
        )


class PredefinedQueryExtractor:
    """검증된 추출 명세만 실행합니다. SQL Planning이나 Text-to-SQL은 수행하지 않습니다."""

    def __init__(self, engine: Engine, spec: dict[str, Any]) -> None:
        self.engine = engine
        self.spec = spec

    def extract(
        self,
        resource_type: str,
        patient_key: Any = None,
        encounter_keys: list[Any] | None = None,
        *,
        patient_keys: list[Any] | None = None,
    ) -> SourceBatch:
        resource_spec = self.spec["resources"][resource_type]
        if patient_key is not None and patient_keys is not None:
            raise ValueError("patient_key와 patient_keys는 동시에 지정할 수 없습니다.")
        if patient_keys is not None:
            patient_keys = _checked_keys(patient_keys)
        elif patient_key is not None:
            _checked_keys([patient_key])
        use_encounters = encounter_keys is not None
        patient_filter = "multiple" if patient_keys is not None else (
            "single" if patient_key is not None else "all"
        )
        statement = text(build_select(
            resource_spec, with_encounters=use_encounters, patient_filter=patient_filter,
        ))
        count_statement = text(build_select(
            resource_spec, with_encounters=use_encounters, patient_filter=patient_filter, count=True,
        ))
        parameters: dict[str, Any] = {}
        if patient_keys is not None:
            statement = statement.bindparams(bindparam("patient_keys", expanding=True))
            count_statement = count_statement.bindparams(bindparam("patient_keys", expanding=True))
            parameters["patient_keys"] = patient_keys
        elif patient_key is not None:
            parameters["patient_key"] = patient_key
        if use_encounters:
            statement = statement.bindparams(
                bindparam("encounter_keys", expanding=True)
            )
            count_statement = count_statement.bindparams(bindparam("encounter_keys", expanding=True))
            parameters["encounter_keys"] = list(encounter_keys or [])
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                if connection.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                expected_count = int(connection.execute(count_statement, parameters).scalar_one())
                rows = connection.execution_options(stream_results=True).execute(statement, parameters)
                try:
                    records = [dict(row) for row in rows.mappings()]
                finally:
                    rows.close()
            finally:
                transaction.rollback()
        return SourceBatch(
            dataset=str(self.spec["dataset"]),
            dataset_version=str(self.spec["dataset_version"]),
            source_resource=str(resource_spec["source_resource"]),
            schema_version=str(self.spec["schema_version"]),
            records=tuple(records),
            metadata={
                "columns": list(resource_spec["columns"]),
                **_source_structure(resource_spec),
            },
            extraction={
                "extractor_version": str(self.spec.get("extractor_version", "v1")),
                "mode": "predefined-query",
                "resource_type": resource_type,
                "expected_count": expected_count,
                "extracted_count": len(records),
                "limit": resource_spec.get("limit"),
                "limit_reached": resource_spec.get("limit") is not None
                    and len(records) >= resource_spec["limit"],
                "truncated": len(records) < expected_count,
            },
        )


class CSVRecordExtractor:
    """Read original CSV fields without FHIR aliases or clinical normalization.

    Headers are normalized to lowercase for compatibility with the same extract
    specification used after PostgreSQL import. Values, dates, UUIDs and empty
    strings remain exactly as exported; original header spelling is retained in
    batch metadata. No event/encounter limit is applied by the CSV extractor.
    """

    def __init__(self, csv_directory: str | Path, spec: dict[str, Any]) -> None:
        self.csv_directory = Path(csv_directory).resolve()
        self.spec = spec

    def extract(
        self, resource_type: str, patient_keys: list[Any] | None = None,
    ) -> SourceBatch:
        resource_spec = self.spec["resources"][resource_type]
        selected_keys = None if patient_keys is None else {
            str(key) for key in _checked_keys(patient_keys)
        }
        filename = str(resource_spec.get("csv_file") or f"{resource_spec['source_resource']}.csv")
        path = (self.csv_directory / filename).resolve()
        if not path.is_relative_to(self.csv_directory):
            raise ValueError("CSV 경로는 입력 디렉터리 안에 있어야 합니다.")
        patient_column = str(resource_spec["patient_column"]).strip().lower()
        records: list[dict[str, Any]] = []
        total_rows = 0
        file_hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_hasher.update(chunk)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            original_columns = list(reader.fieldnames or [])
            columns = [column.strip().lower() for column in original_columns]
            if not columns or len(set(columns)) != len(columns):
                raise ValueError("CSV 헤더가 비어 있거나 정규화 후 중복됩니다.")
            if "_source_record_id" in columns:
                raise ValueError("_source_record_id는 추출기 예약 컬럼입니다.")
            required = {str(column).lower() for column in resource_spec["columns"]}
            missing = required - set(columns)
            if missing:
                raise ValueError(f"CSV 필수 컬럼 누락: {sorted(missing)}")
            if patient_column not in columns:
                raise ValueError(f"환자 키 컬럼 누락: {patient_column}")
            seen_patients: set[str] = set()
            for row_number, raw in enumerate(reader, start=2):
                total_rows += 1
                if None in raw or any(value is None for value in raw.values()):
                    raise ValueError(f"CSV 행의 컬럼 개수가 잘못되었습니다: {row_number}")
                row = {normalized: raw[original]
                       for original, normalized in zip(original_columns, columns)}
                key = row[patient_column]
                if not key:
                    raise ValueError(f"CSV 환자 키가 비어 있습니다: {row_number}")
                if resource_type == "Patient":
                    if key in seen_patients:
                        raise ValueError(f"Patient CSV에 중복 환자 키가 있습니다: {key}")
                    seen_patients.add(key)
                if selected_keys is not None and key not in selected_keys:
                    continue
                row["_source_record_id"] = f"{path.relative_to(self.csv_directory).as_posix()}:{row_number}"
                records.append(row)
        return SourceBatch(
            dataset=str(self.spec["dataset"]),
            dataset_version=str(self.spec.get("dataset_version", "unspecified")),
            source_resource=str(resource_spec["source_resource"]),
            schema_version=str(self.spec["schema_version"]),
            records=tuple(records),
            metadata={
                "columns": [*columns, "_source_record_id"],
                "record_identity_fields": ["_source_record_id"],
                "original_columns": original_columns,
                "column_normalization": dict(zip(original_columns, columns)),
                **_source_structure(resource_spec),
                "values_normalized": False,
                "generator": dict(self.spec.get("generator") or {}),
            },
            extraction={
                "extractor_version": str(self.spec.get("extractor_version", "v1")),
                "mode": "csv-records",
                "resource_type": resource_type,
                "source_file": filename,
                "source_sha256": file_hasher.hexdigest(),
                "total_source_rows": total_rows,
                "expected_count": len(records),
                "extracted_count": len(records),
                "limit": None,
                "limit_reached": False,
                "truncated": False,
            },
        )
