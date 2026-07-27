import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.llm_query_service import (
    EncounterQueryContext,
    LlmClinicalQueryService,
)
from fhir_agent_pipeline.sql_agent import (
    NaturalLanguageSqlAgent,
    SqlAnswer,
    SqlGenerationError,
    SqlResultValidationError,
    SqlSchemaContext,
)


class _FakeLlm:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def chat(self, _system: str, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return json.dumps(
            {
                "sql": f"SELECT patient_id FROM patient_table /* attempt {self.calls} */",
                "explanation": "test",
            }
        )


class _FakeGateway:
    def __init__(self, always_wrong: bool = False) -> None:
        self.always_wrong = always_wrong
        self.calls = 0

    def validate(self, sql: str) -> str:
        return f"SAFE({sql})"

    def execute(self, sql: str):
        self.calls += 1
        patient_id = 8 if self.always_wrong or self.calls == 1 else 7
        return f"SAFE({sql})", [{"patient_id": patient_id}]


def _agent(always_wrong: bool = False) -> NaturalLanguageSqlAgent:
    agent = object.__new__(NaturalLanguageSqlAgent)
    agent.settings = load_settings(require_database=False)
    agent.gateway = _FakeGateway(always_wrong)
    agent.llm = _FakeLlm()
    agent._schema_context = lambda _question: SqlSchemaContext(
        content="patient_table(patient_id)",
        rag_requested=True,
        rag_used=True,
        retrieved_document_count=1,
        fallback_reason=None,
    )
    return agent


def _patient_scope(rows):
    if rows[0]["patient_id"] != 7:
        raise SqlResultValidationError(
            "result_patient_scope", "requested patient is 7"
        )


def test_sql_result_validation_error_is_self_corrected() -> None:
    agent = _agent()

    answer = agent.ask("patient 7", result_validator=_patient_scope)

    assert answer.llm_calls_used == 2
    assert answer.failure_categories == ("result_patient_scope",)
    assert [item.category for item in answer.attempts] == [
        "result_patient_scope",
        None,
    ]
    assert "Previous SQL" in agent.llm.prompts[1]
    assert "requested patient is 7" in agent.llm.prompts[1]
    assert answer.rag_used


def test_final_sql_failure_preserves_every_attempt() -> None:
    agent = _agent(always_wrong=True)

    with pytest.raises(SqlGenerationError) as captured:
        agent.ask(
            "patient 7",
            max_attempts=2,
            result_validator=_patient_scope,
        )

    assert len(captured.value.attempts) == 2
    assert all(
        item.category == "result_patient_scope"
        for item in captured.value.attempts
    )


def test_database_execution_error_is_self_corrected() -> None:
    agent = _agent()
    original_execute = agent.gateway.execute
    calls = 0

    def execute(sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SQLAlchemyError("unknown column")
        return original_execute(sql)

    agent.gateway.execute = execute

    answer = agent.ask("patient 7")

    assert answer.llm_calls_used == 2
    assert answer.failure_categories == ("database_execution",)
    assert answer.attempts[0].error


def test_safety_policy_error_is_self_corrected() -> None:
    agent = _agent()
    original_validate = agent.gateway.validate
    calls = 0

    def validate(sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("unlisted table")
        return original_validate(sql)

    agent.gateway.validate = validate

    answer = agent.ask("patient 7")

    assert answer.llm_calls_used == 2
    assert answer.failure_categories == ("safety_policy",)


def test_execution_semantics_error_is_self_corrected() -> None:
    agent = _agent()
    validations = 0

    def validate_execution(_sql, _rows):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise SqlResultValidationError(
                "result_encounter_scope_narrowing",
                "dependent relation narrowed encounter coverage",
            )

    answer = agent.ask(
        "all encounters for patient 7",
        execution_validator=validate_execution,
    )

    assert answer.llm_calls_used == 2
    assert answer.failure_categories == (
        "result_encounter_scope_narrowing",
    )
    assert "broadest canonical encounter" in agent.llm.prompts[1]


def test_missing_exact_alias_error_adds_focused_correction() -> None:
    agent = _agent()
    validations = 0

    def validate_result(_rows):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise SqlResultValidationError(
                "result_required_columns",
                "missing: _patient_key",
            )

    answer = agent.ask("condition rows", result_validator=validate_result)

    assert answer.llm_calls_used == 2
    assert answer.failure_categories == ("result_required_columns",)
    assert "including its leading underscore" in agent.llm.prompts[1]


def test_semantic_relationship_retry_accepts_confirmed_empty_source() -> None:
    class Catalog:
        schemas = {"alpha"}

    class Gateway:
        catalog = Catalog()

    class Agent:
        gateway = Gateway()

        def ask(self, question, result_validator, execution_validator=None):
            if execution_validator:
                execution_validator(
                    "SELECT * FROM (SELECT 1 FROM alpha.event) AS q",
                    [],
                )
            with pytest.raises(SqlResultValidationError) as captured:
                result_validator([])
            assert captured.value.category == "result_relationship_empty"
            result_validator([])
            return SqlAnswer(
                question=question,
                sql="SELECT * FROM (SELECT 1 FROM alpha.event) AS q",
                rows=[],
                explanation="원천 데이터 부재 확인",
                llm_calls_used=2,
                failure_categories=("result_relationship_empty",),
            )

    service = LlmClinicalQueryService(
        load_settings(require_database=False),
        sql_agent=Agent(),
    )
    context = EncounterQueryContext(
        keys=("VISIT-1",),
        source_relations=("alpha.visit",),
        sql="SELECT visit_key AS _encounter_key FROM alpha.visit",
        schema_hint="alpha.visit: person_key, visit_key",
    )

    result = service.query(
        "Condition",
        "PERSON-1",
        context,
        None,
        semantic_relationship_retry=True,
        previous_sql="SELECT * FROM alpha.event WHERE visit_key = 'VISIT-1'",
    )

    assert result.rows == []
    assert result.trace.semantic_relationship_retry
    assert result.trace.encounter_context_relations == ("alpha.visit",)
    assert "alpha.visit" in result.trace.question
    assert "Previous zero-row SQL" in result.trace.question
