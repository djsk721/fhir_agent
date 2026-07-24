from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from string import Formatter
from typing import Any


@dataclass(frozen=True)
class QueryProfile:
    """FHIR 리소스 하나의 원천 테이블 역할과 LLM SQL 질문 템플릿."""

    source_table_role: str
    template: str
    dictionary_table_role: str | None = None

    def render(self, values: dict[str, Any]) -> str:
        placeholders = {
            field_name
            for _, field_name, _, _ in Formatter().parse(self.template)
            if field_name
        }
        missing = placeholders - set(values)
        if missing:
            raise ValueError(
                f"SQL 질문 템플릿 변수 누락: {sorted(missing)}"
            )
        return self.template.format(**values)


@dataclass(frozen=True)
class CohortProfile:
    """환자 cohort를 읽는 방식. 현재는 FHIR Patient NDJSON provider를 지원합니다."""

    provider: str
    patient_file_patterns: tuple[str, ...]
    identifier_system: str | None
    key_type: str


@dataclass(frozen=True)
class SourceProfile:
    """병원별 DB 구조·조회·식별 정책을 공통 Pipeline 밖에서 관리합니다."""

    name: str
    description: str
    database_schema: str
    tables: dict[str, str]
    mapping_path: Path
    query_profiles: dict[str, QueryProfile]
    bundle_namespace: str
    cohort: CohortProfile
    source_path: Path

    @classmethod
    def load(cls, path: Path) -> "SourceProfile":
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"SOURCE_PROFILE_PATH 파일을 찾을 수 없습니다: {resolved}")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        base = resolved.parent
        query_path = (base / str(payload["query_profiles"])).resolve()
        mapping_path = (base / str(payload["mapping"])).resolve()
        if not query_path.is_file():
            raise FileNotFoundError(f"query profile 파일을 찾을 수 없습니다: {query_path}")
        if not mapping_path.is_file():
            raise FileNotFoundError(f"mapping 파일을 찾을 수 없습니다: {mapping_path}")
        query_payload = json.loads(query_path.read_text(encoding="utf-8"))
        queries = {
            str(resource_type): QueryProfile(
                source_table_role=str(value["source_table_role"]),
                dictionary_table_role=(
                    str(value["dictionary_table_role"])
                    if value.get("dictionary_table_role")
                    else None
                ),
                template=str(value["template"]),
            )
            for resource_type, value in query_payload.items()
        }
        cohort_payload = payload.get("cohort", {})
        profile = cls(
            name=str(payload["name"]),
            description=str(payload.get("description", "")),
            database_schema=str(payload["database"]["schema"]),
            tables={
                str(role): str(table)
                for role, table in payload["database"]["tables"].items()
            },
            mapping_path=mapping_path,
            query_profiles=queries,
            bundle_namespace=str(
                payload.get("bundle_namespace", payload["name"])
            ),
            cohort=CohortProfile(
                provider=str(cohort_payload.get("provider", "none")),
                patient_file_patterns=tuple(
                    str(item)
                    for item in cohort_payload.get("patient_file_patterns", [])
                ),
                identifier_system=(
                    str(cohort_payload["identifier_system"])
                    if cohort_payload.get("identifier_system")
                    else None
                ),
                key_type=str(cohort_payload.get("key_type", "string")),
            ),
            source_path=resolved,
        )
        profile._validate()
        return profile

    def _validate(self) -> None:
        required_roles = {
            query.source_table_role
            for query in self.query_profiles.values()
        } | {
            query.dictionary_table_role
            for query in self.query_profiles.values()
            if query.dictionary_table_role
        }
        missing_roles = required_roles - set(self.tables)
        if missing_roles:
            raise ValueError(
                f"source profile database.tables 역할 누락: {sorted(missing_roles)}"
            )
        if self.cohort.key_type not in {"integer", "string"}:
            raise ValueError("cohort.key_type은 integer 또는 string이어야 합니다.")
        if self.cohort.provider == "fhir-patient-ndjson" and not (
            self.cohort.patient_file_patterns
        ):
            raise ValueError(
                "fhir-patient-ndjson provider에는 patient_file_patterns가 필요합니다."
            )

    def query_for(self, resource_type: str) -> QueryProfile:
        try:
            return self.query_profiles[resource_type]
        except KeyError as exc:
            raise ValueError(
                f"{self.name} profile에 {resource_type} query가 없습니다."
            ) from exc

    def table(self, role: str) -> str:
        try:
            return self.tables[role]
        except KeyError as exc:
            raise ValueError(
                f"{self.name} profile에 table 역할 {role!r}가 없습니다."
            ) from exc

