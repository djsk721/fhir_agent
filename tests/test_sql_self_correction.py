import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.db import (
    SourceColumn,
    SourceSchemaCatalog,
    SourceTable,
)
from fhir_agent_pipeline.sql_agent import (
    NaturalLanguageSqlAgent,
    normalize_output_aliases,
    normalize_resource_key_separators,
    repair_encounter_identity,
    repair_invented_projection_literals,
    repair_scoped_encounter_key,
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


def test_identity_correction_precedes_long_schema_hint() -> None:
    agent = _agent()
    agent.gateway.schema_hint = lambda _sql: "x" * 10_000
    validations = 0

    def validate(_rows):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise SqlResultValidationError(
                "result_resource_identity",
                "rows=17, unique_keys=13, duplicate_key_counts={'k': 2}",
            )

    answer = agent.ask("procedure rows", result_validator=validate)

    assert answer.llm_calls_used == 2
    correction_prompt = agent.llm.prompts[1]
    assert "duplicate_key_counts={'k': 2}" in correction_prompt
    assert "documented primary-key columns" in correction_prompt
    assert "sequence, order, timestamp, or code columns" in correction_prompt
    assert correction_prompt.index("duplicate_key_counts") < correction_prompt.index(
        "Exact columns"
    )


def test_repeated_identity_sql_gets_catalog_driven_fallback() -> None:
    agent = object.__new__(NaturalLanguageSqlAgent)
    agent.settings = load_settings(require_database=False)

    class Gateway:
        catalog = SourceSchemaCatalog((
            SourceTable(
                "clinical",
                "event",
                (
                    SourceColumn("sequence", "INTEGER", False),
                    SourceColumn("code", "TEXT", False),
                ),
                (),
            ),
        ))

        def validate(self, sql):
            return f"SAFE({sql})"

        def execute(self, sql):
            if "md5(to_jsonb(e)::text)" in sql:
                return f"SAFE({sql})", [
                    {"_resource_key": "a"},
                    {"_resource_key": "b"},
                ]
            return f"SAFE({sql})", [
                {"_resource_key": "same"},
                {"_resource_key": "same"},
            ]

        def schema_hint(self, _sql):
            return "clinical.event: sequence, code"

    class Llm:
        def chat(self, _system, _prompt):
            return json.dumps({
                "sql": (
                    "SELECT e.code AS _resource_key, e.code AS _code "
                    "FROM clinical.event AS e"
                ),
                "explanation": "test",
            })

    validations = 0

    def validate(rows):
        nonlocal validations
        validations += 1
        keys = [row["_resource_key"] for row in rows]
        if len(keys) != len(set(keys)):
            raise SqlResultValidationError(
                "result_resource_identity", "duplicate key"
            )

    agent.gateway = Gateway()
    agent.llm = Llm()
    agent._schema_context = lambda _question: SqlSchemaContext(
        content="clinical.event: sequence, code",
        rag_requested=True,
        rag_used=True,
        retrieved_document_count=1,
        fallback_reason=None,
    )

    answer = agent.ask("event rows", result_validator=validate)

    assert answer.llm_calls_used == 2
    assert validations == 2
    assert "md5(to_jsonb(e)::text)" in answer.sql


def test_invented_projection_literal_uses_unique_catalog_column() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "encounters",
            (
                SourceColumn("patient_id", "INTEGER", False),
                SourceColumn("encounter_id", "INTEGER", True),
                SourceColumn("admission_type", "TEXT", False),
            ),
            (),
        ),
    ))
    sql = (
        "SELECT e.patient_id AS _patient_key, "
        "CASE WHEN e.encounter_id IS NOT NULL "
        "THEN 'Admission' ELSE NULL END AS _encounter_type "
        "FROM clinical.encounters AS e"
    )

    repaired = repair_invented_projection_literals(sql, catalog)

    assert "e.admission_type AS _encounter_type" in repaired
    assert "'Admission'" not in repaired


def test_invented_projection_repair_removes_empty_concat() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "encounters",
            (SourceColumn("encounter_id", "INTEGER", True),),
            (),
        ),
    ))

    repaired = repair_invented_projection_literals(
        "SELECT e.encounter_id::text || '' AS _encounter_key "
        "FROM clinical.encounters AS e",
        catalog,
    )

    assert "|| ''" not in repaired


def test_descriptive_aliases_follow_source_contract_order() -> None:
    sql = (
        'SELECT p.id AS "patient key", p.sex AS "administrative gender", '
        'p.death_at AS "death date/time" FROM clinical.patient AS p'
    )

    repaired = normalize_output_aliases(
        sql,
        ("_patient_key", "_gender", "_death_datetime"),
    )

    assert "p.id AS _patient_key" in repaired
    assert "p.sex AS _gender" in repaired
    assert "p.death_at AS _death_datetime" in repaired
    assert '"patient key"' not in repaired


def test_existing_contract_aliases_are_not_reordered() -> None:
    sql = (
        "SELECT p.id AS _patient_key, p.sex AS _gender, "
        "NULL AS _death_datetime FROM clinical.patient AS p"
    )

    assert normalize_output_aliases(
        sql,
        ("_patient_key", "_gender", "_death_datetime"),
    ) == sql


def test_repeated_resource_key_separator_is_normalized() -> None:
    sql = (
        "SELECT e.patient_id AS _patient_key, "
        "e.visit_id::text || '||' || e.code AS _resource_key "
        "FROM clinical.event AS e"
    )

    repaired = normalize_resource_key_separators(sql)

    assert "'||'" not in repaired
    assert "'|'" in repaired


def test_encounter_identity_uses_only_metadata_proven_unused_column() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "visits",
            (
                SourceColumn("person_id", "INTEGER", False),
                SourceColumn("visit_id", "INTEGER", False),
                SourceColumn("visit_type", "TEXT", False),
                SourceColumn("started_at", "TIMESTAMP", False),
                SourceColumn("ended_at", "TIMESTAMP", False),
            ),
            (),
        ),
    ))
    sql = (
        "SELECT v.person_id AS _patient_key, "
        "CAST(v.visit_id AS TEXT) || CAST(NULL AS TEXT) "
        "AS _encounter_key, "
        "v.visit_type AS _encounter_type, "
        "v.started_at AS _start_datetime, "
        "v.ended_at AS _end_datetime "
        "FROM clinical.visits AS v"
    )

    repaired = repair_encounter_identity(sql, catalog)

    assert (
        'CAST(v."visit_id" AS TEXT) AS _encounter_key'
        in repaired
    )
    assert "CAST(NULL AS TEXT)" not in repaired


def test_true_composite_encounter_identity_is_not_guessed() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "visits",
            (
                SourceColumn("person_id", "INTEGER", False),
                SourceColumn("facility_id", "INTEGER", False),
                SourceColumn("visit_number", "INTEGER", False),
                SourceColumn("visit_type", "TEXT", False),
            ),
            (),
        ),
    ))
    sql = (
        "SELECT v.person_id AS _patient_key, "
        "v.facility_id::text || '-' || v.visit_number::text "
        "AS _encounter_key, "
        "v.visit_type AS _encounter_type "
        "FROM clinical.visits AS v"
    )

    assert repair_encounter_identity(sql, catalog) == sql


def test_encounter_key_uses_scope_predicate_column() -> None:
    sql = (
        "SELECT e.patient_id AS _patient_key, "
        "CAST(e.visit_id AS TEXT) || CAST(e.sequence AS TEXT) "
        "AS _encounter_key, e.code AS _resource_key "
        "FROM clinical.event AS e "
        "WHERE CAST(e.visit_id AS TEXT) IN ('11', '12')"
    )

    repaired = repair_scoped_encounter_key(sql, (11, 12))

    assert (
        "CAST(e.visit_id AS TEXT) AS _encounter_key"
        in repaired
    )
    assert "CAST(e.sequence AS TEXT) AS _encounter_key" not in repaired


def test_encounter_scope_repair_ignores_unrelated_in_predicate() -> None:
    sql = (
        "SELECT e.patient_id AS _patient_key, "
        "e.sequence::text AS _encounter_key, "
        "e.code AS _resource_key "
        "FROM clinical.event AS e "
        "WHERE e.status IN ('final', 'amended') "
        "AND e.visit_id IN (11, 12)"
    )

    repaired = repair_scoped_encounter_key(sql, (11, 12))

    assert "CAST(e.visit_id AS TEXT) AS _encounter_key" in repaired
    assert "CAST(e.status AS TEXT) AS _encounter_key" not in repaired
