from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .db import build_engine
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


@dataclass(frozen=True)
class SqlAnswer:
    question: str
    sql: str
    rows: list[dict[str, Any]]
    explanation: str
    llm_calls_used: int = 1
    failure_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class SqlSafetyPolicy:
    allowed_schemas: frozenset[str]
    allowed_tables: frozenset[str]
    max_rows: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "SqlSafetyPolicy":
        return cls(
            frozenset(item.lower() for item in settings.allowed_schemas),
            frozenset(item.lower() for item in settings.allowed_tables),
            settings.sql_max_rows,
        )

    def apply(self, sql: str) -> str:
        cleaned = sql.strip().rstrip(";")
        masked = _STRING_LITERAL.sub("''", cleaned)
        self._validate_statement(masked)
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
            if table not in self.allowed_tables:
                raise ValueError(f"허용되지 않은 테이블입니다: {table}")
            if schema and schema not in self.allowed_schemas:
                raise ValueError(f"허용되지 않은 스키마입니다: {schema}")


class SafeSqlGateway:
    """LLM SQL을 보수적으로 검사하고 읽기 전용 트랜잭션에서만 실행합니다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)
        self.policy = SqlSafetyPolicy.from_settings(settings)

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

    def _schema_context(self, question: str) -> str:
        if self.settings.vectors_enabled:
            try:
                store = PgVectorKnowledgeStore(self.settings, self.gateway.engine)
                results = store.search(self.settings.sql_vector_table, question)
                if results:
                    return "\n\n".join(item["content"] for item in results)
            except Exception:
                pass
        return "\n\n".join(doc.content for doc in database_schema_documents(self.settings, self.gateway.engine))

    def ask(self, question: str, max_attempts: int | None = None) -> SqlAnswer:
        context = self._schema_context(question)
        allowed = ", ".join(self.settings.allowed_tables)
        attempts = max_attempts or self.settings.llm_sql_max_attempts
        previous_error = ""
        failure_categories: list[str] = []
        for attempt in range(1, attempts + 1):
            correction = (
                "\nPrevious SQL attempt failed. Correct it using this error:\n"
                f"{previous_error[:1000]}\n"
                if previous_error
                else ""
            )
            prompt = f"""Database schema context:
{context}

User question: {question}
Allowed tables: {allowed}
{correction}
Return JSON only with these fields:
- sql: one PostgreSQL SELECT statement
- explanation: short Korean explanation
Never invent columns. Do not use DDL/DML.
"""
            try:
                response = self.llm.chat(
                    "You translate clinical questions into safe, read-only PostgreSQL. Return JSON only.",
                    prompt,
                )
                try:
                    payload = load_llm_json(response)
                    proposed_sql = str(payload["sql"])
                except (KeyError, TypeError, ValueError) as exc:
                    failure_categories.append("response_format")
                    raise exc
                try:
                    sql, rows = self.gateway.execute(proposed_sql)
                except ValueError as exc:
                    failure_categories.append("safety_policy")
                    raise exc
                except SQLAlchemyError as exc:
                    failure_categories.append("database_execution")
                    raise exc
                return SqlAnswer(
                    question=question,
                    sql=sql,
                    rows=rows,
                    explanation=str(payload.get("explanation", "")),
                    llm_calls_used=attempt,
                    failure_categories=tuple(failure_categories),
                )
            except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
                previous_error = f"{type(exc).__name__}: {exc}"
                if attempt >= attempts:
                    raise RuntimeError(
                        f"LLM SQL 생성이 {attempts}회 모두 실패했습니다: {previous_error}"
                    ) from exc
        raise AssertionError("SQL generation loop must return or raise")
