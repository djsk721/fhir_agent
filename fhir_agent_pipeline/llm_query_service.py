from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .resource_registry import adapter_for
from .source_contract import FHIR_SOURCE_ALIASES
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
        question = self._question(
            resource_type, patient_key, encounter_keys, encounter_override
        )
        answer = self.sql_agent.ask(
            question,
            result_validator=lambda rows: self._validate_result(
                resource_type, rows, patient_key, encounter_keys
            ),
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
        if resource_type not in {"Patient", "Encounter"} and not encounter_keys:
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
            "limit": {
                "Encounter": self.settings.max_encounters,
                "Condition": self.settings.max_conditions_per_encounter
                * max(1, len(encounter_keys)),
                "Procedure": self.settings.max_procedures_per_encounter
                * max(1, len(encounter_keys)),
                "Observation": self.settings.max_observations_per_encounter
                * max(1, len(encounter_keys)),
            }.get(resource_type, 1),
        }
        return self.profile.query_for(resource_type).render(values)

    def _validate_result(
        self,
        resource_type: str,
        rows: list[dict[str, Any]],
        patient_key: int | str,
        encounter_keys: list[int | str],
    ) -> None:
        if not rows and adapter_for(resource_type).source_required:
            raise SqlResultValidationError(
                "result_empty_required",
                f"LLM SQL이 필수 {resource_type} 행을 반환하지 않았습니다.",
            )
        if not rows:
            return
        required = set(FHIR_SOURCE_ALIASES[resource_type])
        missing = required - set(rows[0])
        if missing:
            raise SqlResultValidationError(
                "result_required_columns",
                f"{resource_type} SQL 결과 alias 누락: {sorted(missing)}",
            )
        allowed_encounters = {str(value) for value in encounter_keys}
        if resource_type not in {"Patient", "Encounter"}:
            keys = [str(row.get("_resource_key")) for row in rows]
            if len(keys) != len(set(keys)) or any(
                key in {"", "None"} for key in keys
            ):
                raise SqlResultValidationError(
                    "result_resource_identity",
                    f"{resource_type} _resource_key는 각 행에서 비어 있지 않고 고유해야 합니다.",
                )
            if any(row.get("_code") in (None, "") for row in rows):
                raise SqlResultValidationError(
                    "result_required_value",
                    f"{resource_type} _code는 실제 원천 컬럼 값이어야 합니다.",
                )
        for index, row in enumerate(rows):
            if str(row.get("_patient_key")) != str(patient_key):
                raise SqlResultValidationError(
                    "result_patient_scope",
                    f"{resource_type}[{index}]가 요청 환자 범위를 벗어났습니다.",
                )
            if resource_type not in {"Patient", "Encounter"} and (
                str(row.get("_encounter_key")) not in allowed_encounters
            ):
                raise SqlResultValidationError(
                    "result_encounter_scope",
                    f"{resource_type}[{index}]가 선택 Encounter 범위를 벗어났습니다.",
                )
