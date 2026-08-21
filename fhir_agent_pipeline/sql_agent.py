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
from .resource_definition import RESOURCE_DEFINITIONS
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
_SOURCE_ALIASES = {
    alias
    for definition in RESOURCE_DEFINITIONS.values()
    for alias in definition.source_aliases
}
_REQUIRED_SOURCE_ALIASES = {
    alias
    for definition in RESOURCE_DEFINITIONS.values()
    for alias in definition.required_source_aliases
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


def _split_projection(projection: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    index = 0
    while index < len(projection):
        char = projection[index]
        if char == "'":
            if (
                in_string
                and index + 1 < len(projection)
                and projection[index + 1] == "'"
            ):
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                parts.append(projection[start:index])
                start = index + 1
        index += 1
    parts.append(projection[start:])
    return parts


def normalize_output_aliases(
    sql: str,
    expected_aliases: tuple[str, ...] | None,
) -> str:
    """요청 순서대로 반환된 설명형 alias를 source-row 계약 alias로 정규화합니다."""

    if not expected_aliases:
        return sql
    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    start = select_matches[-1].end()
    from_match = re.search(r"\bFROM\b", sql[start:], re.IGNORECASE)
    if not from_match:
        return sql
    end = start + from_match.start()
    parts = _split_projection(sql[start:end])
    if len(parts) != len(expected_aliases):
        return sql

    alias_pattern = re.compile(
        r"\bAS\s+(?P<alias>\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)\s*$",
        re.IGNORECASE,
    )
    matches = [alias_pattern.search(part) for part in parts]
    if any(match is None for match in matches):
        return sql
    current_aliases = {
        match.group("alias").strip('"').lower()
        for match in matches
        if match is not None
    }
    expected = {alias.lower() for alias in expected_aliases}
    if current_aliases == expected or current_aliases & expected:
        return sql

    normalized = [
        alias_pattern.sub(f"AS {expected_alias}", part)
        for part, expected_alias in zip(parts, expected_aliases)
    ]
    return sql[:start] + ",".join(normalized).rstrip() + "\n" + sql[end:]


def normalize_resource_key_separators(sql: str) -> str:
    """복합 resource key의 반복 구분자를 허용된 단일 구분자로 축약합니다."""

    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    start = select_matches[-1].end()
    from_match = re.search(r"\bFROM\b", sql[start:], re.IGNORECASE)
    if not from_match:
        return sql
    end = start + from_match.start()
    parts = _split_projection(sql[start:end])
    identity_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.search(
                r"\bAS\s+\"?_resource_key\"?\s*$",
                part,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if identity_index is None:
        return sql

    def normalize_literal(match: re.Match[str]) -> str:
        value = match.group(1)
        if len(value) > 1 and set(value) <= {"-", ":", "|"}:
            return f"'{value[0]}'"
        return match.group(0)

    parts[identity_index] = re.sub(
        r"'((?:''|[^'])*)'",
        normalize_literal,
        parts[identity_index],
    )
    return sql[:start] + ",".join(parts).rstrip() + "\n" + sql[end:]


def repair_encounter_identity(
    sql: str,
    catalog: SourceSchemaCatalog,
) -> str:
    """Encounter key 식에서 metadata로 입증되는 단일 식별 컬럼을 복원합니다."""

    alias_tables: dict[str, Any] = {}
    for match in _RELATION_ALIAS.finditer(sql):
        schema, table, alias = (
            value.strip('"') for value in match.groups()
        )
        relation = catalog.get(schema, table)
        if relation is not None:
            alias_tables[alias.lower()] = relation
    if not alias_tables:
        return sql

    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    start = select_matches[-1].end()
    from_match = re.search(r"\bFROM\b", sql[start:], re.IGNORECASE)
    if not from_match:
        return sql
    end = start + from_match.start()
    parts = _split_projection(sql[start:end])
    identity_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.search(
                r"\bAS\s+\"?_encounter_key\"?\s*$",
                part,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if identity_index is None:
        return sql

    column_pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\."
        r"\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
        re.IGNORECASE,
    )
    used_elsewhere = {
        (alias.lower(), column.lower())
        for index, part in enumerate(parts)
        if index != identity_index
        for alias, column in column_pattern.findall(part)
    }
    candidates: list[tuple[str, str]] = []
    for alias, column in column_pattern.findall(parts[identity_index]):
        normalized_alias = alias.lower()
        normalized_column = column.lower()
        relation = alias_tables.get(normalized_alias)
        if relation is None:
            continue
        if normalized_column not in {
            item.name.lower() for item in relation.columns
        }:
            continue
        candidate = (normalized_alias, column)
        if (
            (normalized_alias, normalized_column) not in used_elsewhere
            and candidate not in candidates
        ):
            candidates.append(candidate)
    if len(candidates) != 1:
        return sql

    alias, column = candidates[0]
    escaped = column.replace('"', '""')
    indentation = re.match(r"\s*", parts[identity_index]).group(0)
    parts[identity_index] = (
        f'{indentation}CAST({alias}."{escaped}" AS TEXT) '
        "AS _encounter_key"
    )
    return sql[:start] + ",".join(parts).rstrip() + "\n" + sql[end:]


def neutralize_invented_optional_aliases(
    sql: str,
    required_aliases: set[str] | None = None,
) -> str:
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
        if (
            alias in _SOURCE_ALIASES
            and alias not in (required_aliases or set())
            and disallowed_literals
        ):
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
    required_aliases: set[str] | None = None,
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
        protected = (
            required_aliases
            if required_aliases is not None
            else _REQUIRED_SOURCE_ALIASES
        )
        if (
            output_alias in _SOURCE_ALIASES
            and output_alias not in protected
            and invalid
        ):
            indentation = re.match(r"\s*", part).group(0)
            normalized.append(f"{indentation}NULL AS {output_alias}")
        else:
            normalized.append(part)
    return sql[:start] + ",".join(normalized).rstrip() + "\n" + sql[end:]


def repair_undefined_aliases(
    sql: str,
    catalog: SourceSchemaCatalog,
    required_aliases: set[str] | None = None,
) -> str:
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
    sql = _neutralize_unverified_optional_columns(
        sql, alias_columns, required_aliases
    )

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


def repair_repeated_resource_identity(
    sql: str,
    catalog: SourceSchemaCatalog,
) -> str:
    """반복 실패 SQL의 event-row key를 catalog 근거로 결정적으로 보강합니다."""

    alias_tables: dict[str, Any] = {}
    for match in _RELATION_ALIAS.finditer(sql):
        schema, table, alias = (
            value.strip('"') for value in match.groups()
        )
        relation = catalog.get(schema, table)
        if relation is not None:
            alias_tables[alias.lower()] = relation
    if not alias_tables:
        return sql

    select_matches = list(re.finditer(r"\bSELECT\b", sql, re.IGNORECASE))
    if not select_matches:
        return sql
    start = select_matches[-1].end()
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
            if (
                in_string
                and index + 1 < len(projection)
                and projection[index + 1] == "'"
            ):
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

    identity_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.search(
                r"\bAS\s+\"?_resource_key\"?\s*$",
                part,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if identity_index is None:
        return sql

    code_part = next(
        (
            part
            for part in parts
            if re.search(
                r"\bAS\s+\"?_code\"?\s*$", part, re.IGNORECASE
            )
        ),
        "",
    )
    identity_part = parts[identity_index]
    source_alias = next(
        (
            alias
            for alias in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\."
                r"\"?[A-Za-z_][A-Za-z0-9_]*\"?",
                code_part + "\n" + identity_part,
            )
            if alias.lower() in alias_tables
        ),
        None,
    )
    if source_alias is None:
        return sql

    relation = alias_tables[source_alias.lower()]
    primary_keys = [
        column.name for column in relation.columns if column.primary_key
    ]
    if len(primary_keys) == 1:
        expression = (
            f'{source_alias}."{primary_keys[0].replace(chr(34), chr(34) * 2)}"'
            "::text"
        )
    elif primary_keys:
        columns = ", ".join(
            f'{source_alias}."{name.replace(chr(34), chr(34) * 2)}"::text'
            for name in primary_keys
        )
        expression = f"concat_ws('|', {columns})"
    else:
        # PK metadata가 없는 relation은 source event row 자체를 사용합니다.
        # join된 dictionary row는 포함하지 않아 join 증식이 계속 검출됩니다.
        expression = f"md5(to_jsonb({source_alias})::text)"

    indentation = re.match(r"\s*", identity_part).group(0)
    parts[identity_index] = (
        f"{indentation}{expression} AS _resource_key"
    )
    return sql[:start] + ",".join(parts).rstrip() + "\n" + sql[end:]


def repair_invented_projection_literals(
    sql: str,
    catalog: SourceSchemaCatalog,
) -> str:
    """발명된 projection 값을 유일한 catalog 근거 컬럼으로 교체합니다."""

    alias_tables: dict[str, Any] = {}
    for match in _RELATION_ALIAS.finditer(sql):
        schema, table, alias = (
            value.strip('"') for value in match.groups()
        )
        relation = catalog.get(schema, table)
        if relation is not None:
            alias_tables[alias.lower()] = relation
    if not alias_tables:
        return sql

    pattern = re.compile(
        r"(?P<expression>"
        r"\bCASE\b.*?\bEND"
        r"|'(?:''|[^'])*'"
        r")\s+AS\s+\"?(?P<output_alias>_[A-Za-z0-9_]+)\"?",
        re.IGNORECASE | re.DOTALL,
    )

    def replacement(match: re.Match[str]) -> str:
        output_alias = match.group("output_alias")
        suffix = output_alias.strip("_").split("_")[-1].lower()
        candidates = [
            (table_alias, column.name)
            for table_alias, relation in alias_tables.items()
            for column in relation.columns
            if column.name.lower().replace("_", "").endswith(suffix)
        ]
        if len(candidates) != 1:
            return match.group(0)
        table_alias, column_name = candidates[0]
        return f"{table_alias}.{column_name} AS {output_alias}"

    repaired = pattern.sub(replacement, sql)
    repaired = re.sub(r"\s*\|\|\s*''", "", repaired)
    return re.sub(r"''\s*\|\|\s*", "", repaired)


def repair_scoped_encounter_key(
    sql: str,
    encounter_scope: tuple[int | str, ...] | None = None,
) -> str:
    """요청 Encounter 집합과 일치하는 IN predicate 컬럼을 출력에 재사용합니다."""

    scope_matches = list(re.finditer(
        r"(?:CAST\s*\(\s*)?"
        r"(?P<table_alias>[A-Za-z_][A-Za-z0-9_]*)\."
        r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+AS\s+[A-Za-z_][A-Za-z0-9_]*\s*\))?"
        r"\s+IN\s*\((?P<values>[^()]*)\)",
        sql,
        re.IGNORECASE,
    ))
    if not scope_matches:
        return sql

    scope_match: re.Match[str] | None = None
    expected = (
        {str(value) for value in encounter_scope}
        if encounter_scope
        else set()
    )
    if expected:
        matching: list[re.Match[str]] = []
        for candidate in scope_matches:
            raw_values = candidate.group("values")
            if re.search(r"\bSELECT\b", raw_values, re.IGNORECASE):
                continue
            parsed = {
                (
                    token[1:-1].replace("''", "'")
                    if token.startswith("'")
                    else token
                )
                for token in re.findall(
                    r"'(?:''|[^'])*'|[-+]?\d+(?:\.\d+)?",
                    raw_values,
                )
            }
            if parsed == expected:
                matching.append(candidate)
        if len(matching) == 1:
            scope_match = matching[0]
    if scope_match is None and len(scope_matches) == 1:
        scope_match = scope_matches[0]
    if scope_match is None:
        return sql

    select_match = re.search(
        r"\bSELECT\b(?P<projection>.*?)\bFROM\b",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if select_match is None:
        return sql
    projection = select_match.group("projection")
    parts = _split_projection(projection)
    encounter_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.search(
                r"\bAS\s+\"?_encounter_key\"?\s*$",
                part,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if encounter_index is None:
        return sql

    indentation = re.match(r"\s*", parts[encounter_index]).group(0)
    table_alias = scope_match.group("table_alias")
    column = scope_match.group("column")
    parts[encounter_index] = (
        f"{indentation}CAST({table_alias}.{column} AS TEXT) "
        "AS _encounter_key"
    )
    repaired_projection = ",".join(parts)
    start, end = select_match.span("projection")
    return sql[:start] + repaired_projection + sql[end:]


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
        required_aliases: set[str] | None = None,
        expected_aliases: tuple[str, ...] | None = None,
        encounter_scope: tuple[int | str, ...] | None = None,
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
        previous_category = ""
        failure_categories: list[str] = []
        attempt_traces: list[SqlAttemptTrace] = []
        for attempt in range(1, attempts + 1):
            correction = (
                "\nPrevious SQL attempt failed. Correct the SQL using both the "
                "previous statement and validation error.\n"
                f"Previous SQL:\n{previous_sql[:4000]}\n"
                f"Validation error and required correction:\n"
                f"{previous_error[:4000]}\n"
                "Do not return a previous failed SQL statement unchanged.\n"
                if previous_error
                else ""
            )
            prompt = f"""Database schema context:
{context.content}

User question: {question}
Allowed schema-qualified tables discovered from PostgreSQL: {allowed}
{correction}
Return JSON only with this field:
- sql: one PostgreSQL SELECT statement
Never invent columns. Do not use DDL/DML.
Always qualify source tables as schema.table.
Every clinical output value must come from a documented source column. Never
emit placeholder text or another quoted string literal as a SELECT output;
return NULL when the source has no value.
Use PostgreSQL syntax only. Build a composite _resource_key with source-column
CAST(... AS TEXT) expressions joined by || and separator literals. Do not hash
the key and do not use TO_HEX, TO_TEXT, TO_JSON_STRING, or another
non-PostgreSQL conversion function.
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
                    execution_sql = normalize_output_aliases(
                        proposed_sql, expected_aliases
                    )
                    catalog = getattr(self.gateway, "catalog", None)
                    if isinstance(catalog, SourceSchemaCatalog):
                        if (
                            required_aliases
                            and "_encounter_key" in required_aliases
                            and "_resource_key" not in required_aliases
                        ):
                            execution_sql = repair_encounter_identity(
                                execution_sql, catalog
                            )
                        if (
                            required_aliases
                            and {
                                "_encounter_key",
                                "_resource_key",
                            } <= required_aliases
                        ):
                            execution_sql = repair_scoped_encounter_key(
                                execution_sql,
                                encounter_scope,
                            )
                        execution_sql = repair_invented_projection_literals(
                            execution_sql, catalog
                        )
                        if (
                            previous_category == "result_resource_identity"
                            and proposed_sql.strip() == previous_sql.strip()
                        ):
                            execution_sql = repair_repeated_resource_identity(
                                execution_sql, catalog
                            )
                        execution_sql = repair_undefined_aliases(
                            execution_sql, catalog, required_aliases
                        )
                    execution_sql = normalize_resource_key_separators(
                        execution_sql
                    )
                    execution_sql = neutralize_invented_optional_aliases(
                        execution_sql, required_aliases
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
                guidance = ""
                if (
                    isinstance(exc, ValueError)
                    and "임의 문자열 상수" in str(exc)
                ):
                    guidance += (
                        "Correction required: remove every quoted string literal "
                        "from SELECT output expressions, regardless of alias. "
                        "For each output alias, select an actual documented "
                        "source column or return NULL when that alias is optional. "
                        "Required aliases such as encounter type and clinical "
                        "code must use a documented non-null source column. "
                        "Only single-character '-', ':' and '|' separators "
                        "inside a composite _resource_key are allowed.\n"
                    )
                if (
                    isinstance(exc, SqlResultValidationError)
                    and exc.category == "result_required_value"
                ):
                    guidance += (
                        "Correction required: a required output alias was NULL. "
                        "Select an actual documented source column with that "
                        "semantic role from the chosen clinical relation. If the "
                        "relation has no such column, choose another relation. "
                        "Do not return NULL for the required alias again.\n"
                    )
                if (
                    isinstance(exc, SqlResultValidationError)
                    and exc.category == "result_resource_identity"
                ):
                    guidance += (
                        "Correction required: _resource_key must identify each "
                        "returned event row, not only its parent patient, visit, "
                        "encounter, code, or stay. Rebuild it from the selected "
                        "event relation's documented primary-key columns when "
                        "available. Otherwise combine documented event-level ID, "
                        "sequence, order, timestamp, or code columns until the "
                        "key is stable, non-null, and unique. Preserve distinct "
                        "source events; do not hide collisions with DISTINCT, "
                        "GROUP BY, row_number, or a result-position value. Also "
                        "check that dictionary joins do not multiply source rows.\n"
                    )
                previous_error = (
                    f"{type(exc).__name__}: {exc}\n"
                    + guidance
                    + (
                        "Exact columns for referenced and structurally related "
                        "tables:\n"
                        + focused_schema[:2500]
                        + "\n"
                        if focused_schema
                        else ""
                    )
                )
                previous_sql = proposed_sql
                previous_category = category
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
