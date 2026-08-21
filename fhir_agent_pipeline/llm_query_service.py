from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .resource_definition import definition_for
from .source_profile import SourceProfile
from .sql_agent import (
    NaturalLanguageSqlAgent,
    SqlAttemptTrace,
    SqlResultValidationError,
)


@dataclass(frozen=True)
class LlmQueryTrace:
    resource_type: str
    question: str
    sql: str
    explanation: str
    row_count: int
    llm_calls_used: int
    failure_categories: tuple[str, ...] = ()
    attempts: tuple[SqlAttemptTrace, ...] = ()
    rag_requested: bool = False
    rag_used: bool = False
    retrieved_document_count: int = 0
    retrieval_fallback_reason: str | None = None


@dataclass(frozen=True)
class QueriedResourceRows:
    trace: LlmQueryTrace
    rows: list[dict[str, Any]]


def strip_database_padding(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.strip() if isinstance(value, str) else value
            for key, value in row.items()
        }
        for row in rows
    ]


class LlmClinicalQueryService:
    """Vector DB의 실제 스키마만 사용해 데이터 소스 중립 SQL을 생성합니다."""

    def __init__(
        self,
        settings: Settings,
        sql_agent: NaturalLanguageSqlAgent | None = None,
        profile: SourceProfile | None = None,
    ) -> None:
        self.settings = settings
        self.profile = profile or SourceProfile.load(settings.source_profile_path)
        self.sql_agent = sql_agent or NaturalLanguageSqlAgent(settings)

    @property
    def engine(self) -> Any:
        return self.sql_agent.gateway.engine

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _sql_literal(value: int | str) -> str:
        if isinstance(value, bool):
            raise ValueError("환자/Encounter 키에는 boolean을 사용할 수 없습니다.")
        if isinstance(value, int):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"

    def query(
        self,
        resource_type: str,
        patient_key: int | str,
        encounter_keys: list[int | str],
        encounter_override: int | str | None,
    ) -> QueriedResourceRows:
        definition = definition_for(resource_type)
        question = self._question(
            resource_type, patient_key, encounter_keys, encounter_override
        )
        answer = self.sql_agent.ask(
            question,
            result_validator=lambda rows: self._validate_result(
                resource_type, rows, patient_key, encounter_keys
            ),
            required_aliases=set(definition.required_source_aliases),
            expected_aliases=definition.source_aliases,
            encounter_scope=tuple(encounter_keys),
        )
        rows = strip_database_padding(answer.rows)
        return QueriedResourceRows(
            LlmQueryTrace(
                resource_type=resource_type,
                question=question,
                sql=answer.sql,
                explanation=answer.explanation,
                row_count=len(rows),
                llm_calls_used=answer.llm_calls_used,
                failure_categories=answer.failure_categories,
                attempts=answer.attempts,
                rag_requested=answer.rag_requested,
                rag_used=answer.rag_used,
                retrieved_document_count=answer.retrieved_document_count,
                retrieval_fallback_reason=answer.retrieval_fallback_reason,
            ),
            rows,
        )

    def encounter_ids(self, rows: list[dict[str, Any]]) -> list[int | str]:
        return [row["_encounter_key"] for row in rows]

    def _question(
        self,
        resource_type: str,
        patient_key: int | str,
        encounter_keys: list[int | str],
        encounter_override: int | str | None,
    ) -> str:
        definition = definition_for(resource_type)
        if definition.requires_encounter_scope and not encounter_keys:
            raise ValueError(f"{resource_type} 조회 전에 Encounter 행이 필요합니다.")
        values: dict[str, Any] = {
            "source_schemas": ", ".join(sorted(self.sql_agent.gateway.catalog.schemas)),
            "patient_key": self._sql_literal(patient_key),
            "encounter_keys": ", ".join(
                self._sql_literal(value) for value in encounter_keys
            ),
            "encounter_override_clause": (
                " and encounter key = "
                + self._sql_literal(encounter_override)
                if encounter_override is not None
                else ""
            ),
            "limit": definition.query_limit(
                self.settings,
                len(encounter_keys),
            ),
        }
        return self.profile.query_for(resource_type).render(values)

    def _validate_result(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        patient_key: int | str,
        encounter_keys: list[int | str],
    ) -> None:
        definition = definition_for(resource_type)
        if not rows and definition.source_required:
            raise SqlResultValidationError(
                "result_empty_required",
                f"LLM SQL이 필수 {resource_type} 행을 반환하지 않았습니다.",
            )
        if not rows:
            return
        if resource_type == "Patient" and len(rows) != 1:
            raise SqlResultValidationError(
                "result_cardinality",
                "Patient SQL은 정확히 한 행을 반환해야 합니다. "
                f"rows={len(rows)}",
            )
        missing = set(definition.source_aliases) - set(rows[0])
        if missing:
            raise SqlResultValidationError(
                "result_required_columns",
                f"{resource_type} SQL 결과 alias 누락: {sorted(missing)}",
            )
        empty_required = {
            alias: [
                index
                for index, row in enumerate(rows)
                if row.get(alias) in (None, "")
            ]
            for alias in definition.required_source_aliases
        }
        empty_required = {
            alias: indexes
            for alias, indexes in empty_required.items()
            if indexes
        }
        if empty_required:
            raise SqlResultValidationError(
                "result_required_value",
                f"{resource_type} 필수 source 값이 비어 있습니다: "
                f"{empty_required}",
            )
        if definition.has_resource_identity:
            self._validate_resource_identity(resource_type, rows)
        allowed_encounters = {str(value) for value in encounter_keys}
        for index, row in enumerate(rows):
            if str(row.get("_patient_key")) != str(patient_key):
                raise SqlResultValidationError(
                    "result_patient_scope",
                    f"{resource_type}[{index}]가 요청 환자 범위를 벗어났습니다.",
                )
            if definition.requires_encounter_scope and (
                str(row.get("_encounter_key")) not in allowed_encounters
            ):
                raise SqlResultValidationError(
                    "result_encounter_scope",
                    f"{resource_type}[{index}]가 선택 Encounter 범위를 벗어났습니다.",
                )

    @staticmethod
    def _validate_resource_identity(
        resource_type: str,
        rows: list[dict[str, Any]],
    ) -> None:
        raw_keys = [row.get("_resource_key") for row in rows]
        keys = [str(value) for value in raw_keys]
        empty_indexes = [
            index
            for index, value in enumerate(raw_keys)
            if value in (None, "")
        ]
        duplicate_counts = {
            key: count
            for key, count in Counter(keys).items()
            if count > 1
        }
        if not duplicate_counts and not empty_indexes:
            return
        duplicate_sample = dict(
            list(sorted(duplicate_counts.items()))[:10]
        )
        raise SqlResultValidationError(
            "result_resource_identity",
            f"{resource_type} _resource_key는 각 행에서 비어 있지 않고 "
            f"고유해야 합니다. rows={len(rows)}, "
            f"unique_keys={len(set(keys))}, "
            f"empty_row_indexes={empty_indexes[:10]}, "
            f"duplicate_key_counts={duplicate_sample}",
        )
