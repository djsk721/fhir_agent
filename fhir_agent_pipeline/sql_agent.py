from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import build_engine, SourceSchemaCatalog
from .ollama import OllamaClient
from .json_utils import load_llm_json, normalize_database_value
from .vector_store import PgVectorKnowledgeStore, database_schema_documents


_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")
_PROHIBITED_STATEMENT = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|COPY|CALL|DO|"
    r"GRANT|REVOKE|VACUUM|ANALYZE|REFRESH|SET|RESET)\b",
    re.IGNORECASE,
)
_ROW_LOCK = re.compile(
    r"\bFOR\s+(UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b",
    re.IGNORECASE,
)
_UNSAFE_FUNCTION = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|dblink|"
    r"lo_import|lo_export)\s*\(",
    re.IGNORECASE,
)
_CTE = re.compile(
    r'(?:\bWITH\b|,)\s*("?[A-Za-z_][A-Za-z0-9_]*"?)\s+AS\s*\(',
    re.IGNORECASE,
)
_TABLE = re.compile(
    r'\b(?:FROM|JOIN)\s+("?[A-Za-z_][A-Za-z0-9_]*"?)'
    r'(?:\s*\.\s*("?[A-Za-z_][A-Za-z0-9_]*"?))?',
    re.IGNORECASE,
)
_OPTIONAL_SOURCE_ALIASES = {
    "_gender",
    "_death_datetime",
    "_encounter_type",
    "_start_datetime",
    "_end_datetime",
    "_code_system",
    "_display",
    "_event_datetime",
    "_value_number",
    "_value_text",
    "_unit",
    "_unit_system",
    "_unit_code",
    "_reference_low",
    "_reference_high",
    "_interpretation",
}
_RELATION_ALIAS = re.compile(
    r'\b(?:FROM|JOIN)\s+("?[\w]+"?)\s*\.\s*("?[\w]+"?)'
    r'\s+AS\s+("?[\w]+"?)',
    re.IGNORECASE,
)
_NAMED_ALIAS = re.compile(
    r'\b(?:FROM|JOIN)\s+("?[\w]+"?)(?!\s*\.)\s+AS\s+("?[\w]+"?)',
    re.IGNORECASE,
)


def neutralize_invented_optional_aliases(sql: str) -> str:
    """선택 alias 표현의 임의 문자열을 NULL로 바꾸고 원본 SQL은 trace에 남깁니다."""

    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    select_match = select_matches[-1]
    start = select_match.end()
    from_match = re.search(r"\bFROM\b", sql[start:], re.IGNORECASE)
    if not from_match:
        return sql
    end = start + from_match.start()
    projection = sql[start:end]
    parts: list[str] = []
    part_start = 0
    depth = 0
    in_string = False
    index = 0
    while index < len(projection):
        char = projection[index]
        if char == "'":
            if in_string and index + 1 < len(projection) and projection[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                parts.append(projection[part_start:index])
                part_start = index + 1
        index += 1
    parts.append(projection[part_start:])

    normalized: list[str] = []
    for part in parts:
        alias_match = re.search(
            r"\bAS\s+(_[A-Za-z0-9_]+)\s*$", part, re.IGNORECASE
        )
        alias = alias_match.group(1).lower() if alias_match else ""
        disallowed_literals = [
            literal
            for literal in _STRING_LITERAL.findall(part)
            if literal not in {"'-'", "':'", "'|'"}
        ]
        if alias in _OPTIONAL_SOURCE_ALIASES and disallowed_literals:
            indentation = re.match(r"\s*", part).group(0)
            normalized.append(f"{indentation}NULL AS {alias}")
        else:
            normalized.append(part)
    return sql[:start] + ",".join(normalized).rstrip() + "\n" + sql[end:]


def _cte_output_columns(sql: str) -> dict[str, set[str]]:
    """괄호 범위를 확정할 수 있는 CTE의 SELECT 출력 컬럼만 추출합니다."""

    outputs: dict[str, set[str]] = {}
    for match in _CTE.finditer(sql):
        name = match.group(1).strip('"').lower()
        opening = sql.find("(", match.start(), match.end())
        if opening < 0:
            continue
        depth = 0
        in_string = False
        closing = -1
        index = opening
        while index < len(sql):
            char = sql[index]
            if char == "'":
                if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                in_string = not in_string
            elif not in_string:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            index += 1
        if closing < 0:
            continue
        body = sql[opening + 1 : closing]
        select = re.search(
            r"\bSELECT\b(.*?)\bFROM\b",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if not select:
            continue
        projection = select.group(1)
        columns = {
            alias.lower()
            for alias in re.findall(
                r"\bAS\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
                projection,
                re.IGNORECASE,
            )
        }
        columns.update(
            column.lower()
            for column in re.findall(
                r"(?:^|,)\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
                r"\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*(?=,|$)",
                projection,
                re.MULTILINE,
            )
        )
        outputs[name] = columns
    return outputs


def _neutralize_unverified_optional_columns(
    sql: str,
    alias_columns: dict[str, set[str]],
) -> str:
    """metadata로 확인되지 않는 컬럼이 든 선택 출력 식은 NULL로 낮춥니다."""

    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    start = select_matches[-1].end()
    from_match = re.search(r"\bFROM\b", sql[start:], re.IGNORECASE)
    if not from_match:
        return sql
    end = start + from_match.start()
    projection = sql[start:end]
    parts = re.split(r",(?=(?:[^()]|\([^()]*\))*$)", projection)
    normalized: list[str] = []
    for part in parts:
        alias_match = re.search(
            r"\bAS\s+(_[A-Za-z0-9_]+)\s*$",
            part,
            re.IGNORECASE,
        )
        output_alias = alias_match.group(1).lower() if alias_match else ""
        invalid = any(
            source_alias.lower() in alias_columns
            and column.lower() not in alias_columns[source_alias.lower()]
            for source_alias, column in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\."
                r"([A-Za-z_][A-Za-z0-9_]*)",
                part,
            )
        )
        if output_alias in _OPTIONAL_SOURCE_ALIASES and invalid:
            indentation = re.match(r"\s*", part).group(0)
            normalized.append(f"{indentation}NULL AS {output_alias}")
        else:
            normalized.append(part)
    return sql[:start] + ",".join(normalized).rstrip() + "\n" + sql[end:]


def repair_undefined_aliases(sql: str, catalog: SourceSchemaCatalog) -> str:
    """명백히 잘못된 alias.column만 DB metadata를 근거로 교정합니다."""

    alias_columns: dict[str, set[str]] = {}
    for match in _RELATION_ALIAS.finditer(sql):
        schema, table, alias = (
            value.strip('"') for value in match.groups()
        )
        relation = catalog.get(schema, table)
        if relation is not None:
            alias_columns[alias.lower()] = {
                name.lower() for name in relation.column_names
            }

    cte_columns = _cte_output_columns(sql)
    for match in _NAMED_ALIAS.finditer(sql):
        relation_name, alias = (
            value.strip('"').lower() for value in match.groups()
        )
        if relation_name in cte_columns:
            alias_columns[alias] = cte_columns[relation_name]
    if not alias_columns:
        return sql
    sql = _neutralize_unverified_optional_columns(sql, alias_columns)

    equality_edges: dict[str, set[tuple[str, str]]] = {}
    for left_alias, left_column, right_alias, right_column in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
        r"\s*=\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
        sql,
        re.IGNORECASE,
    ):
        if left_column.lower() != right_column.lower():
            continue
        equality_edges.setdefault(left_column.lower(), set()).add(
            (left_alias.lower(), right_alias.lower())
        )

    def equivalent_candidates(column: str, candidates: list[str]) -> bool:
        if len(candidates) < 2:
            return False
        graph: dict[str, set[str]] = {}
        for left, right in equality_edges.get(column, set()):
            graph.setdefault(left, set()).add(right)
            graph.setdefault(right, set()).add(left)
        reachable = {candidates[0]}
        pending = [candidates[0]]
        while pending:
            current = pending.pop()
            for neighbor in graph.get(current, set()) - reachable:
                reachable.add(neighbor)
                pending.append(neighbor)
        return set(candidates) <= reachable

    qualified_replacements: dict[tuple[str, str], str] = {}
    for alias, column in re.findall(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)',
        sql,
    ):
        normalized_alias = alias.lower()
        normalized_column = column.lower()
        if (
            normalized_alias in alias_columns
            and normalized_column in alias_columns[normalized_alias]
        ):
            continue
        candidates = [
            known_alias
            for known_alias, columns in alias_columns.items()
            if normalized_column in columns
        ]
        if len(candidates) == 1:
            qualified_replacements[(alias, column)] = candidates[0]
        elif (
            normalized_alias not in alias_columns
            and equivalent_candidates(normalized_column, candidates)
        ):
            qualified_replacements[(alias, column)] = sorted(candidates)[0]
    for (alias, column), replacement in qualified_replacements.items():
        sql = re.sub(
            rf"\b{re.escape(alias)}\.{re.escape(column)}\b",
            f"{replacement}.{column}",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


@dataclass(frozen=True)
class SqlAttemptTrace:
    attempt: int
    proposed_sql: str
    safe_sql: str
    category: str | None
    error: str | None
    row_count: int | None


class SqlResultValidationError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class SqlGenerationError(RuntimeError):
    def __init__(
        self, message: str, attempts: tuple[SqlAttemptTrace, ...]
    ) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class SqlSchemaContext:
    content: str
    rag_requested: bool
    rag_used: bool
    retrieved_document_count: int
    fallback_reason: str | None


@dataclass(frozen=True)
class SqlAnswer:
    question: str
    sql: str
    rows: list[dict[str, Any]]
    explanation: str
    llm_calls_used: int = 1
    failure_categories: tuple[str, ...] = ()
    attempts: tuple[SqlAttemptTrace, ...] = ()
    rag_requested: bool = False
    rag_used: bool = False
    retrieved_document_count: int = 0
    retrieval_fallback_reason: str | None = None


@dataclass(frozen=True)
class SqlSafetyPolicy:
    allowed_relations: frozenset[tuple[str, str]]
    max_rows: int

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        catalog: SourceSchemaCatalog | None = None,
    ) -> "SqlSafetyPolicy":
        if catalog is None:
            raise ValueError("SqlSafetyPolicy에는 SourceSchemaCatalog가 필요합니다.")
        if not catalog.relation_names:
            raise ValueError(
                "public/system 스키마를 제외한 원천 테이블을 찾지 못했습니다."
            )
        return cls(
            catalog.relation_names,
            settings.sql_max_rows,
        )

    @property
    def allowed_schemas(self) -> frozenset[str]:
        return frozenset(schema for schema, _ in self.allowed_relations)

    def apply(self, sql: str) -> str:
        cleaned = sql.strip().rstrip(";")
        masked = _STRING_LITERAL.sub("''", cleaned)
        self._validate_statement(masked)
        self._validate_projection_literals(cleaned)
        self._validate_tables(masked)
        return f"SELECT * FROM (\n{cleaned}\n) AS _safe_query\nLIMIT {self.max_rows}"

    @staticmethod
    def _validate_statement(masked: str) -> None:
        if ";" in masked:
            raise ValueError("SQL은 한 문장만 허용됩니다.")
        if re.search(r"--|/\*|\*/|\$\$", masked):
            raise ValueError("SQL 주석과 dollar-quoted 문자열은 허용되지 않습니다.")
        if not re.match(r"^\s*(SELECT|WITH)\b", masked, re.IGNORECASE):
            raise ValueError("SELECT/WITH 조회문만 허용됩니다.")
        if _PROHIBITED_STATEMENT.search(masked):
            raise ValueError("데이터 또는 스키마를 변경하는 SQL은 허용되지 않습니다.")
        if _ROW_LOCK.search(masked):
            raise ValueError("행 잠금 SQL은 허용되지 않습니다.")
        if _UNSAFE_FUNCTION.search(masked):
            raise ValueError("서버 파일/외부 연결/지연 함수는 허용되지 않습니다.")

    @staticmethod
    def _validate_projection_literals(sql: str) -> None:
        for match in re.finditer(
            r"\bSELECT\b(?P<projection>.*?)\bFROM\b",
            sql,
            re.IGNORECASE | re.DOTALL,
        ):
            projection = re.sub(
                r"'(?:-|:|\|)'", "NULL", match.group("projection")
            )
            if _STRING_LITERAL.search(projection):
                raise ValueError(
                    "SELECT 결과에 임의 문자열 상수를 사용할 수 없습니다. "
                    "원천 테이블 컬럼을 사용하고 값이 없으면 NULL을 반환하세요."
                )

    def _validate_tables(self, masked: str) -> None:
        cte_names = {match.group(1).strip('"').lower() for match in _CTE.finditer(masked)}
        matches = list(_TABLE.finditer(masked))
        if not matches:
            raise ValueError("조회 대상 테이블을 식별할 수 없습니다.")
        for match in matches:
            first = match.group(1).strip('"').lower()
            second = (match.group(2) or "").strip('"').lower()
            schema, table = (first, second) if second else ("", first)
            if table in cte_names:
                continue
            if not schema:
                raise ValueError(
                    f"원천 테이블은 허용 스키마로 한정되도록 schema.table 형식이어야 합니다: "
                    f"{table}"
                )
            if (schema, table) not in self.allowed_relations:
                raise ValueError(f"허용되지 않은 원천 relation입니다: {schema}.{table}")


class SafeSqlGateway:
    """LLM SQL을 보수적으로 검사하고 읽기 전용 트랜잭션에서만 실행합니다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)
        self.catalog = SourceSchemaCatalog.load(settings, self.engine)
        self.policy = SqlSafetyPolicy.from_settings(settings, self.catalog)

    def validate(self, sql: str) -> str:
        return self.policy.apply(sql)

    def execute(self, sql: str) -> tuple[str, list[dict[str, Any]]]:
        safe_sql = self.validate(sql)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{self.settings.sql_statement_timeout_ms}ms"},
                )
                result = connection.execute(text(safe_sql)).mappings()
                rows = [
                    {
                        str(key): normalize_database_value(value)
                        for key, value in row.items()
                    }
                    for row in result
                ]
            finally:
                transaction.rollback()
        return safe_sql, rows

    def schema_hint(self, sql: str) -> str:
        """실패 SQL이 참조한 테이블의 실제 컬럼만 교정 문맥으로 제공합니다."""

        relations: set[tuple[str, str]] = set()
        for match in _TABLE.finditer(sql):
            first = match.group(1).strip('"')
            second = (match.group(2) or "").strip('"')
            if not second:
                continue
            schema, table = first, second
            if (
                schema.lower() in self.policy.allowed_schemas
                and (schema.lower(), table.lower()) in self.policy.allowed_relations
            ):
                relations.add((schema, table))
        return self.catalog.schema_hint(relations)


class NaturalLanguageSqlAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gateway = SafeSqlGateway(settings)
        self.llm = OllamaClient(
            settings.ollama_base_url,
            settings.ollama_model,
            timeout=settings.ollama_timeout_seconds,
            context_size=settings.ollama_context_size,
            max_output_tokens=settings.ollama_max_output_tokens,
            max_retries=settings.ollama_max_retries,
            retry_delay_seconds=settings.ollama_retry_delay_seconds,
        )

    def _schema_context(self, question: str) -> SqlSchemaContext:
        fallback_reason: str | None = None
        if self.settings.vectors_enabled:
            try:
                store = PgVectorKnowledgeStore(self.settings, self.gateway.engine)
                results = store.search(
                    self.settings.sql_vector_table,
                    question,
                    profile_scoped=True,
                )
                if results:
                    return SqlSchemaContext(
                        content="\n\n".join(item["content"] for item in results),
                        rag_requested=True,
                        rag_used=True,
                        retrieved_document_count=len(results),
                        fallback_reason=None,
                    )
                fallback_reason = "VectorDB 검색 결과가 없습니다."
            except Exception as exc:
                fallback_reason = f"{type(exc).__name__}: {exc}"
        content = "\n\n".join(
            doc.content
            for doc in database_schema_documents(
                self.settings,
                self.gateway.engine,
                self.gateway.catalog,
            )
        )
        return SqlSchemaContext(
            content=content,
            rag_requested=self.settings.vectors_enabled,
            rag_used=False,
            retrieved_document_count=0,
            fallback_reason=fallback_reason,
        )

    def ask(
        self,
        question: str,
        max_attempts: int | None = None,
        result_validator: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> SqlAnswer:
        context = self._schema_context(question)
        policy = getattr(self.gateway, "policy", None)
        allowed = (
            ", ".join(
                f"{schema}.{table}"
                for schema, table in sorted(policy.allowed_relations)
            )
            if isinstance(policy, SqlSafetyPolicy)
            else "all relations documented in the schema context"
        )
        attempts = max_attempts or self.settings.llm_sql_max_attempts
        previous_error = ""
        previous_sql = ""
        failure_categories: list[str] = []
        attempt_traces: list[SqlAttemptTrace] = []
        for attempt in range(1, attempts + 1):
            correction = (
                "\nPrevious SQL attempt failed. Correct the SQL using both the "
                "previous statement and validation error.\n"
                f"Previous SQL:\n{previous_sql[:4000]}\n"
                f"Validation error:\n{previous_error[:2000]}\n"
                if previous_error
                else ""
            )
            prompt = f"""Database schema context:
{context.content}

User question: {question}
Allowed schema-qualified tables discovered from PostgreSQL: {allowed}
{correction}
Return JSON only with these fields:
- sql: one PostgreSQL SELECT statement
- explanation: short Korean explanation
Never invent columns. Do not use DDL/DML.
Always qualify source tables as schema.table.
Every clinical output value must come from a documented source column. Never
emit placeholder text or another quoted string literal as a SELECT output;
return NULL when the source has no value.
Any quoted literal in the SELECT projection is a hard failure, including
labels used for type, status, category, code system, or display.
Treat requested output names as SELECT aliases, not as evidence that physical
columns with those names exist. Before responding, verify every table.column
used by SELECT, JOIN, WHERE, and ORDER BY appears exactly in the schema context.
When correcting a database error, replace the invalid physical column using the
schema context; changing only the filter syntax is not a correction.
"""
            try:
                category = "unknown"
                proposed_sql = ""
                safe_sql = ""
                rows: list[dict[str, Any]] | None = None
                response = self.llm.chat(
                    "You translate clinical questions into safe, read-only PostgreSQL. Return JSON only.",
                    prompt,
                )
                try:
                    payload = load_llm_json(response)
                    proposed_sql = str(payload["sql"])
                except (KeyError, TypeError, ValueError) as exc:
                    category = "response_format"
                    failure_categories.append(category)
                    raise exc
                try:
                    execution_sql = neutralize_invented_optional_aliases(
                        proposed_sql
                    )
                    catalog = getattr(self.gateway, "catalog", None)
                    if isinstance(catalog, SourceSchemaCatalog):
                        execution_sql = repair_undefined_aliases(
                            execution_sql, catalog
                        )
                    safe_sql = self.gateway.validate(execution_sql)
                    sql, rows = self.gateway.execute(execution_sql)
                except ValueError as exc:
                    category = "safety_policy"
                    failure_categories.append(category)
                    raise exc
                except SQLAlchemyError as exc:
                    category = "database_execution"
                    failure_categories.append(category)
                    raise exc
                if result_validator:
                    try:
                        result_validator(rows)
                    except SqlResultValidationError as exc:
                        category = exc.category
                        failure_categories.append(category)
                        raise
                attempt_traces.append(
                    SqlAttemptTrace(
                        attempt=attempt,
                        proposed_sql=proposed_sql,
                        safe_sql=sql,
                        category=None,
                        error=None,
                        row_count=len(rows),
                    )
                )
                return SqlAnswer(
                    question=question,
                    sql=sql,
                    rows=rows,
                    explanation=str(payload.get("explanation", "")),
                    llm_calls_used=attempt,
                    failure_categories=tuple(failure_categories),
                    attempts=tuple(attempt_traces),
                    rag_requested=context.rag_requested,
                    rag_used=context.rag_used,
                    retrieved_document_count=context.retrieved_document_count,
                    retrieval_fallback_reason=context.fallback_reason,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                SQLAlchemyError,
            ) as exc:
                focused_schema = ""
                schema_hint = getattr(self.gateway, "schema_hint", None)
                if proposed_sql and callable(schema_hint):
                    focused_schema = str(schema_hint(proposed_sql))
                previous_error = (
                    (
                        "Exact columns for referenced and structurally related tables:\n"
                        + focused_schema
                        + "\n"
                    )
                    if focused_schema
                    else ""
                ) + f"{type(exc).__name__}: {exc}"
                if (
                    isinstance(exc, ValueError)
                    and "임의 문자열 상수" in str(exc)
                ):
                    previous_error += (
                        "\nCorrection required: set _code_system, _unit_system "
                        "and _unit_code to NULL unless a documented source column "
                        "stores that value. Build _resource_key only from source "
                        "columns; '-', ':' and '|' separators are allowed."
                    )
                if (
                    isinstance(exc, SqlResultValidationError)
                    and exc.category == "result_required_value"
                ):
                    previous_error += (
                        "\nCorrection required: a required output alias was NULL. "
                        "Select an actual documented source column with that "
                        "semantic role from the chosen clinical relation. If the "
                        "relation has no such column, choose another relation. "
                        "Do not return NULL for the required alias again."
                    )
                if (
                    isinstance(exc, SqlResultValidationError)
                    and exc.category == "result_resource_identity"
                ):
                    previous_error += (
                        "\nCorrection required: _resource_key must identify each "
                        "returned event row, not only its parent patient, visit, "
                        "encounter, or stay. Use a documented event-row key or "
                        "combine enough documented event columns to be non-null "
                        "and unique. Verify uniqueness before returning SQL."
                    )
                previous_sql = proposed_sql
                attempt_traces.append(
                    SqlAttemptTrace(
                        attempt=attempt,
                        proposed_sql=proposed_sql,
                        safe_sql=safe_sql,
                        category=category,
                        error=previous_error,
                        row_count=(
                            len(rows)
                            if rows is not None and category.startswith("result_")
                            else None
                        ),
                    )
                )
                if attempt >= attempts:
                    raise SqlGenerationError(
                        f"LLM SQL 생성이 {attempts}회 모두 실패했습니다: "
                        f"{previous_error}",
                        tuple(attempt_traces),
                    ) from exc
        raise AssertionError("SQL generation loop must return or raise")
