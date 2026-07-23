from fhir_agent_pipeline.config import load_settings
from fhir_agent_pipeline.sql_agent import SqlSafetyPolicy


def policy() -> SqlSafetyPolicy:
    return SqlSafetyPolicy.from_settings(load_settings(require_database=False))


def test_select_is_wrapped_with_row_limit() -> None:
    sql = policy().apply("SELECT subject_id FROM patients")
    assert "AS _safe_query" in sql
    assert sql.endswith("LIMIT 100")


def test_cte_and_qualified_allowed_table() -> None:
    sql = policy().apply(
        "WITH p AS (SELECT subject_id FROM mimiciv_hosp.patients) SELECT * FROM p"
    )
    assert "mimiciv_hosp.patients" in sql


def test_blocks_write_and_unlisted_table() -> None:
    sql_policy = policy()
    for sql in ("DELETE FROM patients", "SELECT * FROM pg_user", "SELECT * FROM patients FOR UPDATE"):
        try:
            sql_policy.apply(sql)
        except ValueError:
            pass
        else:
            raise AssertionError(f"차단되어야 하는 SQL: {sql}")
