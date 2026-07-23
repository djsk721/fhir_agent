import pytest

from fhir_agent_pipeline.json_utils import load_llm_json


def test_load_llm_json_accepts_markdown_fence() -> None:
    assert load_llm_json("```json\n{\"ok\": true}\n```") == {"ok": True}


def test_load_llm_json_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="LLM JSON 응답"):
        load_llm_json("not-json")
