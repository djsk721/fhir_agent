from fhir_agent_pipeline.db import (
    SourceColumn,
    SourceSchemaCatalog,
    SourceTable,
)
from fhir_agent_pipeline.sql_agent import (
    SqlSafetyPolicy,
    neutralize_invented_optional_aliases,
    repair_repeated_resource_identity,
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


def test_repeated_identity_uses_catalog_primary_key_without_source_mapping() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "event",
            (
                SourceColumn("event_id", "INTEGER", True),
                SourceColumn("code", "TEXT", False),
            ),
            (),
        ),
    ))
    sql = (
        "SELECT e.code AS _resource_key, e.code AS _code "
        "FROM clinical.event AS e"
    )

    repaired = repair_repeated_resource_identity(sql, catalog)

    assert 'e."event_id"::text AS _resource_key' in repaired


def test_repeated_identity_hashes_source_row_when_catalog_has_no_pk() -> None:
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
        SourceTable(
            "clinical",
            "dictionary",
            (SourceColumn("code", "TEXT", False),),
            (),
        ),
    ))
    sql = (
        "SELECT e.code AS _resource_key, e.code AS _code, d.code AS _display "
        "FROM clinical.event AS e "
        "JOIN clinical.dictionary AS d ON e.code = d.code"
    )

    repaired = repair_repeated_resource_identity(sql, catalog)

    assert "md5(to_jsonb(e)::text) AS _resource_key" in repaired
    assert "to_jsonb(d)" not in repaired
