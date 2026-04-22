from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from .config import Settings
from .prompts import (
    FHIR_GENERATOR_SYSTEM_PROMPT,
    FHIR_USER_PROMPT_TEMPLATE,
    MANAGER_CORRECTION_PROMPT_TEMPLATE,
    MANAGER_SYSTEM_PROMPT,
    MAPPING_SYSTEM_PROMPT,
    MAPPING_USER_PROMPT_TEMPLATE,
    SQL_PLANNER_SYSTEM_PROMPT,
    SQL_PLANNER_USER_PROMPT_TEMPLATE,
    VERIFIER_SYSTEM_PROMPT,
    VERIFIER_USER_PROMPT_TEMPLATE,
)


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _iter_fenced_blocks(raw_text: str) -> list[str]:
    return [
        match.group("body").strip()
        for match in re.finditer(
            r"```(?:json|JSON)?\s*(?P<body>[\s\S]*?)```",
            raw_text,
        )
    ]


def _extract_first_complete_json(text: str) -> str | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return text[index : index + end]
    return None


def _extract_json_block(raw_text: str) -> str:
    """LLM 응답에서 JSON 본문을 최대한 안전하게 추출합니다."""

    text = raw_text.strip()
    fenced_blocks = _iter_fenced_blocks(text)
    for block in fenced_blocks:
        extracted = _extract_first_complete_json(block)
        if extracted is not None:
            return extracted

    extracted = _extract_first_complete_json(text)
    if extracted is not None:
        return extracted

    if fenced_blocks:
        return fenced_blocks[0]
    return text


def _load_json_response(raw_text: str) -> Any:
    return json.loads(_extract_json_block(raw_text))


@dataclass(frozen=True)
class VerificationResult:
    """Verifier의 구조화된 응답을 표현합니다."""

    ok: bool
    errors: list[str]
    suggestions: list[str]


@dataclass(frozen=True)
class SqlPlanResult:
    resource_type: str
    rationale: str
    tables: list[str]
    sql: str
    params: dict[str, Any]


class BaseAgent:
    """Ollama 기반 LangChain 채팅 모델 호출을 공통화합니다."""

    def __init__(self, settings: Settings, system_prompt: str) -> None:
        self._client = ChatOllama(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            temperature=0,
        )
        self._system_prompt = system_prompt

    def _invoke(self, user_prompt: str) -> str:
        response = self._client.invoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_prompt),
            ]
        )
        return str(response.content)


class MappingAgent(BaseAgent):
    """원시 MIMIC-IV 데이터를 임상 개념 중심 JSON으로 정규화합니다."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, MAPPING_SYSTEM_PROMPT)

    def run(
        self,
        raw_payload: dict[str, Any],
        rag_context: str = "None",
        sql_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = MAPPING_USER_PROMPT_TEMPLATE.format(
            raw_json=_dump_json(raw_payload),
            sql_evidence=_dump_json(sql_evidence or {}),
            rag_context=rag_context,
        )
        return _load_json_response(self._invoke(prompt))


class SqlPlannerAgent(BaseAgent):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, SQL_PLANNER_SYSTEM_PROMPT)

    def run(
        self,
        resource_type: str,
        seed_context: dict[str, Any],
        sql_schema_context: str,
        rag_context: str,
    ) -> SqlPlanResult:
        prompt = SQL_PLANNER_USER_PROMPT_TEMPLATE.format(
            resource_type=resource_type,
            seed_context=_dump_json(seed_context),
            sql_schema_context=sql_schema_context,
            rag_context=rag_context,
        )
        result = _load_json_response(self._invoke(prompt))
        return SqlPlanResult(
            resource_type=str(result.get("resource_type", resource_type)),
            rationale=str(result.get("rationale", "")),
            tables=[str(value) for value in result.get("tables", [])],
            sql=str(result.get("sql", "")).strip(),
            params=dict(result.get("params", {})),
        )


class FhirGeneratorAgent(BaseAgent):
    """정규화된 데이터를 FHIR R4 JSON 리소스로 변환합니다."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, FHIR_GENERATOR_SYSTEM_PROMPT)

    def run(
        self,
        mapping_payload: dict[str, Any],
        resource_type: str,
        correction_instructions: str = "None",
        generated_resource_context: dict[str, Any] | None = None,
        rag_context: str = "None",
    ) -> dict[str, Any]:
        prompt = FHIR_USER_PROMPT_TEMPLATE.format(
            mapping_json=_dump_json(mapping_payload),
            rag_context=rag_context,
            resource_type=resource_type,
            generated_resource_context=_dump_json(generated_resource_context or {}),
            correction_instructions=correction_instructions,
        )
        return _load_json_response(self._invoke(prompt))


class VerifierAgent(BaseAgent):
    """FHIR JSON에 대한 1차 LLM 검토 결과를 생성합니다."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, VERIFIER_SYSTEM_PROMPT)

    def run(
        self,
        fhir_payload: dict[str, Any],
        resource_type: str,
        rag_context: str = "None",
    ) -> VerificationResult:
        prompt = VERIFIER_USER_PROMPT_TEMPLATE.format(
            resource_type=resource_type,
            rag_context=rag_context,
            fhir_json=_dump_json(fhir_payload),
        )
        result = _load_json_response(self._invoke(prompt))
        return VerificationResult(
            ok=bool(result.get("ok", False)),
            errors=list(result.get("errors", [])),
            suggestions=list(result.get("suggestions", [])),
        )


class ManagerAgent(BaseAgent):
    """검증 피드백을 바탕으로 재생성 여부와 수정 지시를 결정합니다."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, MANAGER_SYSTEM_PROMPT)

    def review_feedback(
        self,
        verifier_result: VerificationResult,
        current_fhir_payload: dict[str, Any],
        resource_type: str,
    ) -> dict[str, str]:
        prompt = MANAGER_CORRECTION_PROMPT_TEMPLATE.format(
            resource_type=resource_type,
            verifier_json=_dump_json(
                {
                    "ok": verifier_result.ok,
                    "errors": verifier_result.errors,
                    "suggestions": verifier_result.suggestions,
                }
            ),
            current_fhir_json=_dump_json(current_fhir_payload),
        )
        result = _load_json_response(self._invoke(prompt))
        return {
            "action": str(result.get("action", "regenerate")),
            "correction_instructions": str(
                result.get("correction_instructions", "")
            ).strip(),
        }
