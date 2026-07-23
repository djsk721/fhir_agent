from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import qualified_table
from .mapping_config import MimicMappingConfig, TableColumns
from .sql_agent import NaturalLanguageSqlAgent


@dataclass(frozen=True)
class LlmQueryTrace:
    resource_type: str
    question: str
    sql: str
    explanation: str
    row_count: int
    llm_calls_used: int
    failure_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueriedResourceRows:
    trace: LlmQueryTrace
    rows: list[dict[str, Any]]


def strip_database_padding(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CHAR 계열의 앞뒤 패딩만 제거하고 임상값과 자료형은 유지합니다."""

    return [
        {
            key: value.strip() if isinstance(value, str) else value
            for key, value in row.items()
        }
        for row in rows
    ]


class LlmClinicalQueryService:
    """리소스별 LLM SQL 계획, 안전 실행, 환자 범위 검증을 담당합니다."""

    def __init__(
        self,
        settings: Settings,
        mapping: MimicMappingConfig,
        sql_agent: NaturalLanguageSqlAgent | None = None,
    ) -> None:
        self.settings = settings
        self.mapping = mapping
        self.sql_agent = sql_agent or NaturalLanguageSqlAgent(settings)

    @property
    def engine(self) -> Any:
        return self.sql_agent.gateway.engine

    def close(self) -> None:
        self.engine.dispose()

    def query(
        self,
        resource_type: str,
        subject_id: int,
        admission_ids: list[int],
        admission_id_override: int | None,
    ) -> QueriedResourceRows:
        question = self._question(
            resource_type, subject_id, admission_ids, admission_id_override
        )
        answer = self.sql_agent.ask(question)
        self._validate_scope(resource_type, answer.rows, subject_id, admission_ids)
        rows = self._prepare_rows(resource_type, answer.rows)
        return QueriedResourceRows(
            trace=LlmQueryTrace(
                resource_type=resource_type,
                question=question,
                sql=answer.sql,
                explanation=answer.explanation,
                row_count=len(rows),
                llm_calls_used=answer.llm_calls_used,
                failure_categories=answer.failure_categories,
            ),
            rows=rows,
        )

    def admission_ids(self, rows: list[dict[str, Any]]) -> list[int]:
        column = self.mapping.admission_columns.get("admission_id")
        return [int(row[column]) for row in rows]

    def _question(
        self,
        resource_type: str,
        subject_id: int,
        admission_ids: list[int],
        admission_id_override: int | None,
    ) -> str:
        handlers = {
            "Patient": self._patient_question,
            "Encounter": self._encounter_question,
            "Condition": self._condition_question,
            "Procedure": self._procedure_question,
            "Observation": self._observation_question,
        }
        try:
            handler = handlers[resource_type]
        except KeyError as exc:
            raise ValueError(
                f"지원하지 않는 LLM 조회 리소스입니다: {resource_type}"
            ) from exc
        if resource_type not in {"Patient", "Encounter"} and not admission_ids:
            raise ValueError(f"{resource_type} 조회 전에 Encounter 행이 필요합니다.")
        return handler(subject_id, admission_ids, admission_id_override)

    def _patient_question(
        self, subject_id: int, _admission_ids: list[int], _override: int | None
    ) -> str:
        columns = self.mapping.patient_columns
        table = qualified_table(
            self.settings.mimic_schema, self.settings.mimic_patients_table
        )
        return (
            "Generate a PostgreSQL SELECT for one Patient source row. "
            f"Read {columns.get('subject_id')}, {columns.get('gender')}, "
            f"{columns.get('death_date')} from {table} where "
            f"{columns.get('subject_id')} = {subject_id}. Return exactly these "
            "physical column names and at most one row."
        )

    def _encounter_question(
        self, subject_id: int, _admission_ids: list[int], override: int | None
    ) -> str:
        columns = self.mapping.admission_columns
        table = qualified_table(
            self.settings.mimic_schema, self.settings.mimic_admissions_table
        )
        admission_filter = (
            f" and {columns.get('admission_id')} = {override}"
            if override is not None
            else ""
        )
        return (
            "Generate a PostgreSQL SELECT for Encounter source rows. "
            f"Read {columns.get('subject_id')}, {columns.get('admission_id')}, "
            f"{columns.get('admission_type')}, {columns.get('start_time')}, "
            f"{columns.get('end_time')} from {table} where "
            f"{columns.get('subject_id')} = {subject_id}{admission_filter}. Order by "
            f"{columns.get('start_time')} descending nulls last and limit "
            f"{self.settings.max_encounters}. Return physical column names unchanged."
        )

    def _condition_question(
        self, subject_id: int, admission_ids: list[int], _override: int | None
    ) -> str:
        columns = self.mapping.diagnosis_columns
        source = qualified_table(
            self.settings.mimic_schema, self.settings.mimic_diagnoses_table
        )
        dictionary = qualified_table(
            self.settings.mimic_schema,
            self.settings.mimic_diagnosis_dictionary_table,
        )
        ids = ", ".join(str(value) for value in admission_ids)
        return (
            "Generate a PostgreSQL SELECT for Condition source rows. Join "
            f"{source} as source to {dictionary} as dictionary on "
            f"source.{columns.get('code')} = dictionary.{columns.get('code')} and "
            f"source.{columns.get('version')} = dictionary.{columns.get('version')}. "
            f"Select source.{columns.get('subject_id')}, "
            f"source.{columns.get('admission_id')}, source.{columns.get('sequence')}, "
            f"source.{columns.get('code')}, source.{columns.get('version')}, and "
            f"dictionary.{columns.get('display')} as {columns.get('display')}. "
            f"Filter subject_id = {subject_id} and admission ID in ({ids}). Order by "
            f"admission ID and sequence ascending and limit "
            f"{self.settings.max_conditions_per_encounter * len(admission_ids)}."
        )

    def _procedure_question(
        self, subject_id: int, admission_ids: list[int], _override: int | None
    ) -> str:
        columns = self.mapping.procedure_columns
        source = qualified_table(
            self.settings.mimic_schema, self.settings.mimic_procedures_table
        )
        dictionary = qualified_table(
            self.settings.mimic_schema,
            self.settings.mimic_procedure_dictionary_table,
        )
        ids = ", ".join(str(value) for value in admission_ids)
        return (
            "Generate a PostgreSQL SELECT for Procedure source rows. Join "
            f"{source} as source to {dictionary} as dictionary on "
            f"source.{columns.get('code')} = dictionary.{columns.get('code')} and "
            f"source.{columns.get('version')} = dictionary.{columns.get('version')}. "
            f"Select source.{columns.get('subject_id')}, "
            f"source.{columns.get('admission_id')}, source.{columns.get('sequence')}, "
            f"source.{columns.get('date')}, source.{columns.get('code')}, "
            f"source.{columns.get('version')}, and dictionary.{columns.get('display')} "
            f"as {columns.get('display')}. Filter subject_id = {subject_id} and "
            f"admission ID in ({ids}). Order by admission ID and sequence ascending "
            f"and limit {self.settings.max_procedures_per_encounter * len(admission_ids)}."
        )

    def _observation_question(
        self, subject_id: int, admission_ids: list[int], _override: int | None
    ) -> str:
        columns = self.mapping.lab_columns
        selected_columns = [
            columns.get(name)
            for name in (
                "subject_id", "admission_id", "event_id", "row_id", "item_id",
                "event_time", "numeric_value", "text_value", "unit",
                "reference_low", "reference_high", "interpretation",
            )
        ]
        table = qualified_table(
            self.settings.mimic_schema, self.settings.mimic_labevents_table
        )
        ids = ", ".join(str(value) for value in admission_ids)
        return (
            "Generate a PostgreSQL SELECT for laboratory Observation source rows. "
            f"Select {', '.join(selected_columns)} from {table} where "
            f"{columns.get('subject_id')} = {subject_id} and "
            f"{columns.get('admission_id')} in ({ids}). Order by "
            f"{columns.get('admission_id')} and {columns.get('event_time')} descending "
            f"nulls last and limit "
            f"{self.settings.max_observations_per_encounter * len(admission_ids)}. "
            "Return physical column names unchanged."
        )

    def _columns_for(self, resource_type: str) -> TableColumns:
        return self.mapping.columns_for(resource_type)

    def _validate_scope(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        subject_id: int,
        admission_ids: list[int],
    ) -> None:
        if not rows:
            raise ValueError(f"LLM SQL이 {resource_type} 소스 행을 반환하지 않았습니다.")
        columns = self._columns_for(resource_type)
        subject_column = columns.get("subject_id")
        for index, row in enumerate(rows):
            if subject_column not in row:
                raise ValueError(
                    f"{resource_type} SQL 결과에 {subject_column} 컬럼이 없습니다."
                )
            if int(row[subject_column]) != subject_id:
                raise ValueError(
                    f"{resource_type}[{index}]가 요청 환자 범위를 벗어났습니다."
                )
            if resource_type not in {"Patient", "Encounter"}:
                admission_column = columns.get("admission_id")
                if admission_column not in row:
                    raise ValueError(
                        f"{resource_type} SQL 결과에 {admission_column} 컬럼이 없습니다."
                    )
                if int(row[admission_column]) not in admission_ids:
                    raise ValueError(
                        f"{resource_type}[{index}]가 선택 입원 범위를 벗어났습니다."
                    )

    def _prepare_rows(
        self, resource_type: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared = strip_database_padding(rows)
        if resource_type == "Condition":
            columns = self.mapping.diagnosis_columns
            for row in prepared:
                row[columns.get("code")] = self.mapping.format_diagnosis_code(
                    row[columns.get("code")], row[columns.get("version")]
                )
                row["fhir_icd_system"] = self.mapping.diagnosis_icd_system(
                    row[columns.get("version")]
                )
        elif resource_type == "Procedure":
            columns = self.mapping.procedure_columns
            for row in prepared:
                row[columns.get("code")] = self.mapping.format_procedure_code(
                    row[columns.get("code")], row[columns.get("version")]
                )
                row["fhir_icd_system"] = self.mapping.procedure_icd_system(
                    row[columns.get("version")]
                )
        return prepared
