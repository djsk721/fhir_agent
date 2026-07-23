from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import math
from typing import Any


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def normalize_database_value(value: Any) -> Any:
    """DB 드라이버 값을 LLM 입력과 JSON 출력에 안전한 값으로 바꿉니다."""

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def extract_json_text(raw_text: str) -> str:
    """마크다운이 섞인 LLM 응답에서 JSON 객체/배열 부분을 추출합니다."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()

    starts = [position for position in (text.find("{"), text.find("[")) if position >= 0]
    if not starts:
        return text
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start:] if end < start else text[start : end + 1]


def load_llm_json(raw_text: str) -> Any:
    try:
        return json.loads(extract_json_text(raw_text))
    except json.JSONDecodeError as exc:
        preview = raw_text.strip().replace("\n", " ")[:200]
        raise ValueError(f"LLM JSON 응답을 해석할 수 없습니다: {preview!r}") from exc
