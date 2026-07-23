from __future__ import annotations

from typing import Any

from .config import Settings
from .json_utils import dump_json, load_llm_json
from .ollama import OllamaClient
from .prompts import (
    FHIR_GENERATION_SYSTEM_PROMPT,
    FHIR_GENERATION_USER_PROMPT_TEMPLATE,
)


class FhirResourceGenerationAgent:
    """SQL 조회 행을 최종 FHIR JSON으로 변환하는 LLM 역할입니다."""

    def __init__(self, settings: Settings) -> None:
        self._client = OllamaClient(
            settings.ollama_base_url,
            settings.ollama_model,
            timeout=settings.ollama_timeout_seconds,
            context_size=settings.ollama_context_size,
            max_output_tokens=settings.ollama_max_output_tokens,
            max_retries=settings.ollama_max_retries,
            retry_delay_seconds=settings.ollama_retry_delay_seconds,
        )

    def run(
        self,
        resource_type: str,
        source_rows: list[dict[str, Any]],
        identity_contract: str,
        fhir_context: str,
        previous_payload: Any | None = None,
        validation_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        correction_context = "No previous validation errors."
        if validation_errors:
            correction_context = (
                "The previous output was rejected. Correct every error without inventing data.\n"
                f"Errors:\n{dump_json(validation_errors)}\n"
                f"Previous output:\n{dump_json(previous_payload)}"
            )
        prompt = FHIR_GENERATION_USER_PROMPT_TEMPLATE.format(
            resource_type=resource_type,
            source_rows=dump_json(source_rows),
            identity_contract=identity_contract,
            fhir_context=fhir_context,
            correction_context=correction_context,
        )
        payload = load_llm_json(
            self._client.chat(FHIR_GENERATION_SYSTEM_PROMPT, prompt)
        )
        if not isinstance(payload, dict):
            raise ValueError("FHIR LLM 응답은 JSON 객체여야 합니다.")
        return payload
