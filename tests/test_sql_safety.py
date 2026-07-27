from fhir_agent_pipeline.sql_agent import (
    SqlSafetyPolicy,
    neutralize_invented_optional_aliases,
)


def policy() -> SqlSafetyPolicy:
    return SqlSafetyPolicy(
        allowed_relations=frozenset(
            {
                ("clinical", "person"),
                ("clinical", "visit"),
            }
        ),
        max_rows=100,
    )


def test_select_is_wrapped_with_row_limit() -> None:
    sql = policy().apply("SELECT person_key FROM clinical.person")
    assert "AS _safe_query" in sql
    assert sql.endswith("LIMIT 100")


def test_cte_and_qualified_allowed_table() -> None:
    sql = policy().apply(
        "WITH p AS (SELECT person_key FROM clinical.person) SELECT * FROM p"
    )
    assert "clinical.person" in sql


def test_blocks_write_and_unlisted_table() -> None:
    sql_policy = policy()
    for sql in ("DELETE FROM patients", "SELECT * FROM pg_user", "SELECT * FROM patients FOR UPDATE"):
        try:
            sql_policy.apply(sql)
        except ValueError:
            pass
        else:
            raise AssertionError(f"차단되어야 하는 SQL: {sql}")


def test_blocks_fabricated_string_projection() -> None:
    try:
        policy().apply(
            "SELECT 'placeholder' AS display "
            "FROM clinical.person"
        )
    except ValueError as exc:
        assert "문자열 상수" in str(exc)
    else:
        raise AssertionError("임의 문자열 projection은 차단되어야 합니다.")


def test_optional_alias_invented_literal_is_neutralized() -> None:
    sql = (
        "SELECT code AS _code, "
        "'http://invented.example' AS _code_system, "
        "code || '-' || seq AS _resource_key "
        "FROM clinical.person"
    )

    normalized = neutralize_invented_optional_aliases(sql)

    assert "NULL AS _code_system" in normalized
    assert "code || '-' || seq AS _resource_key" in normalized


def test_optional_alias_guard_handles_outer_select_after_cte() -> None:
    sql = (
        "WITH x AS (SELECT code FROM clinical.person) "
        "SELECT code AS _code, code || ' (invented)' AS _display FROM x"
    )

    normalized = neutralize_invented_optional_aliases(sql)

    assert "NULL AS _display" in normalized
