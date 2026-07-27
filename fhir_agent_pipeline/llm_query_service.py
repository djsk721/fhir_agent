from __future__ import annotations

from collections import Counter
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
    referenced_source_relations,
    row_restricting_relationships,
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
    encounter_context_relations: tuple[str, ...] = ()
    semantic_relationship_retry: bool = False
    normalized_contract_aliases: tuple[str, ...] = ()
    encounter_qualified_resource_keys: int = 0
    requested_row_limit: int = 0
    row_limit_reached: bool = False


@dataclass(frozen=True)
class QueriedResourceRows:
    trace: LlmQueryTrace
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class EncounterQueryContext:
    """Encounter 값과 이를 선택한 동적 SQL lineage를 후속 조회에 전달합니다."""

    keys: tuple[int | str, ...] = ()
    source_relations: tuple[str, ...] = ()
    sql: str = ""
    schema_hint: str = ""

    def prompt_text(self) -> str:
        if not self.keys:
            return "No encounter context is available."
        keys = ", ".join(str(value) for value in self.keys)
        relations = ", ".join(self.source_relations) or "not extracted"
        return (
            f"Selected encounter keys: {keys}\n"
            f"Relations used by the executed Encounter SQL: {relations}\n"
            "Executed Encounter SQL (lineage evidence, not a physical-column "
            f"mapping):\n{self.sql[:4000]}\n"
            "Metadata for the selected and structurally related relations:\n"
            f"{self.schema_hint[:4000] or 'not available'}"
        )


def strip_database_padding(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.strip() if isinstance(value, str) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def normalize_contract_aliases(
    resource_type: str,
    rows: list[dict[str, Any]],
) -> tuple[str, ...]:
    """값은 바꾸지 않고 선행 underscore가 빠진 공통 계약 alias만 복원합니다."""

    expected = set(FHIR_SOURCE_ALIASES[resource_type])
    normalized: set[str] = set()
    for row in rows:
        for source_name in list(row):
            target_name = "_" + source_name.lower().lstrip("_")
            if (
                target_name in expected
                and target_name not in row
                and source_name != target_name
            ):
                row[target_name] = row.pop(source_name)
                normalized.add(f"{source_name}->{target_name}")
    return tuple(sorted(normalized))


def qualify_cross_encounter_resource_keys(
    resource_type: str,
    rows: list[dict[str, Any]],
) -> int:
    """다른 Encounter에서 재사용된 source key만 Encounter 범위로 한정합니다."""

    if resource_type in {"Patient", "Encounter"}:
        return 0
    counts = Counter(str(row.get("_resource_key")) for row in rows)
    duplicate_keys = {
        key for key, count in counts.items()
        if count > 1 and key not in {"", "None"}
    }
    changed = 0
    for row in rows:
        key = str(row.get("_resource_key"))
        encounter_key = row.get("_encounter_key")
        if key in duplicate_keys and encounter_key not in (None, ""):
            row["_resource_key"] = f"{encounter_key}|{key}"
            changed += 1
    return changed


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
        encounter_context: EncounterQueryContext,
        encounter_override: int | str | None,
        *,
        semantic_relationship_retry: bool = False,
        previous_sql: str = "",
    ) -> QueriedResourceRows:
        requested_row_limit = self._row_limit(
            resource_type, len(encounter_context.keys)
        )
        question = self._question(
            resource_type,
            patient_key,
            encounter_context,
            encounter_override,
            semantic_relationship_retry,
            previous_sql,
            requested_row_limit,
        )
        empty_validation_count = 0
        normalized_aliases: set[str] = set()
        qualified_resource_keys = 0

        def validate(rows: list[dict[str, Any]]) -> None:
            nonlocal empty_validation_count, qualified_resource_keys
            normalized_aliases.update(
                normalize_contract_aliases(resource_type, rows)
            )
            qualified_resource_keys = qualify_cross_encounter_resource_keys(
                resource_type, rows
            )
            self._validate_result(
                resource_type,
                rows,
                patient_key,
                list(encounter_context.keys),
            )
            if (
                semantic_relationship_retry
                and resource_type not in {"Patient", "Encounter"}
                and not rows
            ):
                empty_validation_count += 1
                if empty_validation_count == 1:
                    raise SqlResultValidationError(
                        "result_relationship_empty",
                        "Encounter가 존재하지만 관련 임상 리소스 조회가 모두 "
                        "0건이어서 동적 relation 및 join 경로를 재검사해야 합니다.",
                    )

        def validate_execution(
            sql: str, _rows: list[dict[str, Any]]
        ) -> None:
            if resource_type != "Encounter":
                return
            narrowing = row_restricting_relationships(sql)
            if narrowing:
                raise SqlResultValidationError(
                    "result_encounter_scope_narrowing",
                    "Encounter SQL이 일반 방문/입원을 하위 relation 존재 여부로 "
                    f"축소할 수 있는 구조를 사용했습니다: {', '.join(narrowing)}",
                )

        answer = self.sql_agent.ask(
            question,
            result_validator=validate,
            execution_validator=validate_execution,
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
                encounter_context_relations=(
                    referenced_source_relations(answer.sql)
                    if resource_type == "Encounter"
                    else encounter_context.source_relations
                ),
                semantic_relationship_retry=semantic_relationship_retry,
                normalized_contract_aliases=tuple(sorted(normalized_aliases)),
                encounter_qualified_resource_keys=qualified_resource_keys,
                requested_row_limit=requested_row_limit,
                row_limit_reached=(
                    resource_type != "Patient"
                    and len(rows) >= requested_row_limit
                ),
            ),
            rows,
        )

    def encounter_ids(self, rows: list[dict[str, Any]]) -> list[int | str]:
        return [row["_encounter_key"] for row in rows]

    def build_encounter_context(
        self, queried: QueriedResourceRows
    ) -> EncounterQueryContext:
        sql = queried.trace.sql
        schema_hint = ""
        hint = getattr(self.sql_agent.gateway, "schema_hint", None)
        if callable(hint):
            schema_hint = str(hint(sql))
        return EncounterQueryContext(
            keys=tuple(self.encounter_ids(queried.rows)),
            source_relations=referenced_source_relations(sql),
            sql=sql,
            schema_hint=schema_hint,
        )

    def _question(
        self,
        resource_type: str,
        patient_key: int | str,
        encounter_context: EncounterQueryContext,
        encounter_override: int | str | None,
        semantic_relationship_retry: bool,
        previous_sql: str,
        requested_row_limit: int,
    ) -> str:
        encounter_keys = list(encounter_context.keys)
        if resource_type not in {"Patient", "Encounter"} and not encounter_keys:
            raise ValueError(f"{resource_type} 조회 전에 Encounter 행이 필요합니다.")
        retry_instruction = (
            "Semantic relationship re-check is required because the selected "
            "Encounter exists while all requested downstream clinical queries "
            "returned zero rows. Do not assume that the clinical relation stores "
            "the selected encounter key directly. Use only retrieved metadata to "
            "find a documented join path back to the relation selected by the "
            "Encounter SQL. Previous zero-row SQL:\n"
            + previous_sql[:4000]
            if semantic_relationship_retry
            else "No cross-resource semantic retry is requested."
        )
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
            "encounter_context": encounter_context.prompt_text(),
            "relationship_retry_instruction": retry_instruction,
            "limit": requested_row_limit,
        }
        return self.profile.query_for(resource_type).render(values)

    def _row_limit(self, resource_type: str, encounter_count: int) -> int:
        """리소스 설정과 SQL 안전 상한 중 작은 값을 LLM과 실행기에 적용합니다."""
        configured = {
            "Encounter": self.settings.max_encounters,
            "Condition": self.settings.max_conditions_per_encounter
            * max(1, encounter_count),
            "Procedure": self.settings.max_procedures_per_encounter
            * max(1, encounter_count),
            "Observation": self.settings.max_observations_per_encounter
            * max(1, encounter_count),
        }.get(resource_type, 1)
        return min(configured, self.settings.sql_max_rows)

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
        if resource_type == "Encounter":
            keys = [str(row.get("_encounter_key")) for row in rows]
            if len(keys) != len(set(keys)) or any(
                key in {"", "None"} for key in keys
            ):
                raise SqlResultValidationError(
                    "result_encounter_identity",
                    "Encounter _encounter_key는 비어 있지 않고 결과 내에서 "
                    "고유해야 합니다.",
                )
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
