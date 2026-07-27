from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from string import Formatter
from typing import Any


@dataclass(frozen=True)
class QueryProfile:
    """FHIR 리소스 하나의 동적 스키마 탐색용 LLM SQL 질문 템플릿."""

    template: str

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
    """병원별 스키마·조회·식별 정책을 공통 Pipeline 밖에서 관리합니다."""

    name: str
    description: str
    query_path: Path
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
        if not query_path.is_file():
            raise FileNotFoundError(f"query profile 파일을 찾을 수 없습니다: {query_path}")
        query_payload = json.loads(query_path.read_text(encoding="utf-8"))
        queries = {
            str(resource_type): QueryProfile(
                template=str(value["template"]),
            )
            for resource_type, value in query_payload.items()
        }
        cohort_payload = payload.get("cohort", {})
        profile = cls(
            name=str(payload["name"]),
            description=str(payload.get("description", "")),
            query_path=query_path,
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
        supported_providers = {"none", "fhir-patient-ndjson"}
        if self.cohort.provider not in supported_providers:
            raise ValueError(
                f"지원하지 않는 cohort provider입니다: "
                f"{self.cohort.provider!r}"
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

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in (self.source_path, self.query_path):
            digest.update(path.read_bytes())
        return digest.hexdigest()
