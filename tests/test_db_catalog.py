from fhir_agent_pipeline.db import (
    SourceColumn,
    SourceForeignKey,
    SourceSchemaCatalog,
    SourceTable,
)
from fhir_agent_pipeline.sql_agent import (
    SqlSafetyPolicy,
    repair_undefined_aliases,
)
from fhir_agent_pipeline.config import load_settings


def _catalog() -> SourceSchemaCatalog:
    return SourceSchemaCatalog((
        SourceTable(
            "clinical", "visit",
            (
                SourceColumn("visit_id", "INTEGER", True),
                SourceColumn("patient_id", "INTEGER", False),
            ),
            (
                SourceForeignKey(
                    ("patient_id",), "clinical", "patient", ("patient_id",)
                ),
            ),
        ),
    ))


def test_catalog_exposes_schema_qualified_relations() -> None:
    catalog = _catalog()
    assert catalog.schemas == frozenset({"clinical"})
    assert catalog.relation_names == frozenset({("clinical", "visit")})
    assert "visit_id INTEGER PRIMARY KEY" in catalog.tables[0].schema_text()


def test_sql_policy_uses_exact_catalog_relations() -> None:
    policy = SqlSafetyPolicy.from_settings(
        load_settings(require_database=False), _catalog()
    )
    assert policy.allowed_relations == frozenset({("clinical", "visit")})


def test_alias_repair_respects_cte_scope_and_uses_metadata() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "event",
            (
                SourceColumn("person_key", "INTEGER", False),
                SourceColumn("event_code", "TEXT", False),
            ),
            (),
        ),
        SourceTable(
            "clinical",
            "visit",
            (
                SourceColumn("visit_key", "INTEGER", True),
                SourceColumn("person_key", "INTEGER", False),
            ),
            (),
        ),
    ))
    sql = (
        "WITH event_rows AS ("
        "SELECT person_key, event_code FROM clinical.event"
        ") "
        "SELECT E.person_key, V.event_code "
        "FROM event_rows AS E "
        "JOIN clinical.visit AS V ON E.person_key = V.person_key"
    )

    repaired = repair_undefined_aliases(sql, catalog)

    assert "E.person_key" in repaired
    assert "e.event_code" in repaired.lower()


def test_unverified_optional_column_is_removed_without_domain_mapping() -> None:
    catalog = _catalog()
    sql = (
        "SELECT V.visit_id AS _resource_key, "
        "V.made_up_label AS _display "
        "FROM clinical.visit AS V"
    )

    repaired = repair_undefined_aliases(sql, catalog)

    assert "NULL AS _display" in repaired
    assert "made_up_label" not in repaired

    range_sql = (
        "SELECT V.visit_id AS _resource_key, "
        "V.made_up_low AS _reference_low "
        "FROM clinical.visit AS V"
    )
    range_repaired = repair_undefined_aliases(range_sql, catalog)
    assert "NULL AS _reference_low" in range_repaired


def test_undefined_alias_can_use_join_proven_equivalent_column() -> None:
    catalog = SourceSchemaCatalog((
        SourceTable(
            "clinical",
            "event",
            (SourceColumn("person_key", "INTEGER", False),),
            (),
        ),
        SourceTable(
            "clinical",
            "visit",
            (SourceColumn("person_key", "INTEGER", False),),
            (),
        ),
    ))
    sql = (
        "SELECT X.person_key AS _patient_key "
        "FROM clinical.event AS E "
        "JOIN clinical.visit AS V ON E.person_key = V.person_key"
    )

    repaired = repair_undefined_aliases(sql, catalog)

    assert "X.person_key" not in repaired
    assert "e.person_key as _patient_key" in repaired.lower()
